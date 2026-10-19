import pytest

from ai import AIError, GenerationFailed, MaintenanceMode, RateLimited, UserBlocked
from api_providers import ProviderError
from subscriptions import QuotaExceeded
from tests.helpers import FakeResp, ReadTimeout, make_services, run
from utils import split_message


def test_generate_success_stores_history_and_consumes(tmp_path):
    s = make_services(tmp_path, script=[FakeResp(content="olá!")])
    r = run(s.ai.generate(1, 1, 0, "oi", False))
    assert r.text == "olá!" and r.reservation.remaining == 9 and r.reported_model == "m-reportado"
    assert [m["content"] for m in s.db.recent_messages(1, 0, 10)] == ["oi", "olá!"]
    call = s.http.calls[0]
    assert call["json"]["model"] == "modelo-teste"
    assert call["url"].endswith("/chat/completions")


def test_free_limit_blocks_generation(tmp_path):
    s = make_services(tmp_path, {"FREE_MESSAGE_LIMIT": "1"}, script=[FakeResp(), FakeResp()])
    run(s.ai.generate(1, 1, 0, "a", False))
    with pytest.raises(QuotaExceeded):
        run(s.ai.generate(1, 1, 0, "b", False))
    assert len(s.http.calls) == 1  # nenhuma chamada paga após o limite


def test_timeout_refunds_quota_after_retries(tmp_path):
    s = make_services(tmp_path, script=[ReadTimeout(), ReadTimeout(), ReadTimeout()])
    with pytest.raises(GenerationFailed) as e:
        run(s.ai.generate(1, 1, 0, "oi", False))
    assert e.value.kind == "timeout" and len(s.http.calls) == 3  # 1 + 2 tentativas
    assert s.subs.peek(1).remaining == 10  # cota devolvida
    assert s.db.count_messages(1, 0) == 0  # nada gravado


def test_retry_then_success(tmp_path):
    s = make_services(tmp_path, script=[FakeResp(status=503), FakeResp(content="ok")])
    assert run(s.ai.generate(1, 1, 0, "oi", False)).text == "ok"
    assert len(s.http.calls) == 2


def test_rate_limit_header_too_long_is_not_retried(tmp_path):
    s = make_services(tmp_path, script=[FakeResp(status=429, headers={"retry-after": "999"})])
    with pytest.raises(GenerationFailed) as e:
        run(s.ai.generate(1, 1, 0, "oi", False))
    assert e.value.kind == "rate_limit" and len(s.http.calls) == 1


def test_auth_error_is_not_retried_and_message_is_safe(tmp_path):
    s = make_services(tmp_path, script=[FakeResp(status=401)])
    with pytest.raises(GenerationFailed) as e:
        run(s.ai.generate(1, 1, 0, "oi", False))
    assert e.value.kind == "auth" and len(s.http.calls) == 1
    assert "gsk_" not in e.value.user_message and "401" not in e.value.user_message


def test_fallback_on_unavailable_but_not_on_bad_request(tmp_path):
    extra = {"FALLBACK_PROVIDER": "mistral", "MISTRAL_API_KEY": "mk-123456", "FALLBACK_MODEL": "fb-model",
             "API_MAX_RETRIES": "0"}
    s = make_services(tmp_path, extra, script=[FakeResp(status=500), FakeResp(content="via fallback")])
    r = run(s.ai.generate(1, 1, 0, "oi", False))
    assert r.text == "via fallback" and r.provider == "mistral"
    assert "mistral" in s.http.calls[1]["url"] and s.http.calls[1]["json"]["model"] == "fb-model"
    assert s.http.calls[1]["headers"]["Authorization"] == "Bearer mk-123456"

    (tmp_path / "b").mkdir()
    s2 = make_services(tmp_path / "b", extra, script=[FakeResp(status=400), FakeResp(content="nunca")])
    with pytest.raises(GenerationFailed) as e:
        run(s2.ai.generate(1, 1, 0, "oi", False))
    assert e.value.kind == "bad_request" and len(s2.http.calls) == 1


def test_missing_key_or_model_is_config_error(tmp_path):
    s = make_services(tmp_path, {"DEFAULT_MODEL": ""})
    with pytest.raises(GenerationFailed) as e:
        run(s.ai.generate(1, 1, 0, "oi", False))
    assert e.value.kind == "config" and s.http.calls == []
    assert s.subs.peek(1).remaining == 10


def test_secrets_never_sent_to_provider(tmp_path):
    s = make_services(tmp_path, script=[FakeResp()])
    run(s.ai.generate(1, 1, 0, "minha chave é gsk_testkey1234567890abcd e token 123456789:" + "A" * 35, False))
    sent = str(s.http.calls[0]["json"]["messages"])
    assert "gsk_testkey1234567890abcd" not in sent and "AAAAAAAAAAAAAAAAAAAA" not in sent


def test_input_validation(tmp_path):
    s = make_services(tmp_path, {"MAX_INPUT_CHARS": "50"})
    for bad in ("", "   ", "\x00\x01", "x" * 51):
        with pytest.raises(AIError):
            run(s.ai.generate(1, 1, 0, bad, False))
    assert s.subs.peek(1).remaining == 10 and s.http.calls == []


def test_blocked_maintenance_and_rate_limit(tmp_path):
    from security import RateLimiter
    s = make_services(tmp_path, script=[FakeResp(), FakeResp()])
    s.db.block_user(5, "x", 1)
    with pytest.raises(UserBlocked):
        run(s.ai.generate(5, 5, 0, "oi", False))
    s.db.set_setting("maintenance", "1")
    with pytest.raises(MaintenanceMode):
        run(s.ai.generate(6, 6, 0, "oi", False))
    run(s.ai.generate(s.config.owner_id, 1, 0, "oi", False))  # admin passa
    s.db.set_setting("maintenance", "0")
    s.ai.limiter = RateLimiter(1, 60)
    run(s.ai.generate(7, 7, 0, "a", False))
    with pytest.raises(RateLimited):
        run(s.ai.generate(7, 7, 0, "b", False))


def test_history_isolation_between_users_groups_topics(tmp_path):
    s = make_services(tmp_path, script=[FakeResp(content="r1"), FakeResp(content="r2"), FakeResp(content="r3")])
    run(s.ai.generate(1, 1, 0, "segredo-do-usuario-1", False))
    run(s.ai.generate(2, 2, 0, "pergunta do 2", False))
    run(s.ai.generate(1, -100, 9, "no topico 9", True))
    sent_user2 = str(s.http.calls[1]["json"]["messages"])
    sent_topic = str(s.http.calls[2]["json"]["messages"])
    assert "segredo-do-usuario-1" not in sent_user2 and "segredo-do-usuario-1" not in sent_topic


def test_history_limit_applies(tmp_path):
    s = make_services(tmp_path, {"HISTORY_MAX_MESSAGES": "4"}, script=[FakeResp(content=f"r{i}") for i in range(5)])
    for i in range(5):
        run(s.ai.generate(1, 1, 0, f"m{i}", False))
    assert s.db.count_messages(1, 0) == 4
    assert len(s.http.calls[4]["json"]["messages"]) == 1 + 4 + 1  # system + histórico + atual


def test_group_premium_billing_goes_to_group_only_when_eligible(tmp_path):
    s = make_services(tmp_path, {"GROUP_PREMIUM_FEATURE_ENABLED": "true"}, script=[FakeResp(), FakeResp()])
    s.groups.register(-100, "G")
    s.db.set_group_field(-100, "enabled", 1)
    s.subs.grant(-100, "standard", 30, actor=1)
    run(s.ai.generate(1, -100, 0, "oi", True))
    assert s.subs.peek(1).used == 1 and s.subs.peek(-100).used == 0  # não elegível: usuário paga
    s.db.set_group_field(-100, "premium_enabled", 1)
    s.db.set_group_member_count(-100, 900)
    run(s.ai.generate(1, -100, 0, "oi", True))
    assert s.subs.peek(-100).used == 1


def test_split_message_preserves_order_and_limit():
    text = ("palavra " * 1500).strip()
    parts = split_message(text, 4000)
    assert all(len(p) <= 4000 for p in parts) and len(parts) >= 3
    assert " ".join(parts).split() == text.split()
    assert split_message("") == [] and split_message("a" * 9000, 4000) == ["a" * 4000, "a" * 4000, "a" * 1000]


def test_provider_error_message_has_no_body():
    e = ProviderError("auth", "groq", 401)
    assert "Bearer" not in str(e)
