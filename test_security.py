import logging

import pytest

from config import Config, ConfigError
from security import (InputError, InvalidCallback, RateLimiter, RedactingFormatter, parse_callback,
                      redact_secrets, sanitize_user_text)
from tests.helpers import OWNER, TOKEN, make_env


def test_redact_secrets_patterns_and_exact_values():
    s = f"token {TOKEN} key gsk_abcdefghijklmnopqrst Bearer abcdefgh12345 api_key=SEGREDO123 meu-valor-exato"
    out = redact_secrets(s, ["meu-valor-exato"])
    for leaked in (TOKEN, "gsk_abcdefghijklmnopqrst", "abcdefgh12345", "SEGREDO123", "meu-valor-exato"):
        assert leaked not in out
    assert redact_secrets("texto normal") == "texto normal"


def test_logging_formatter_redacts_exceptions():
    fmt = RedactingFormatter("%(message)s", secrets=["supersecreto99"])
    try:
        raise RuntimeError("falha com supersecreto99 e " + TOKEN)
    except RuntimeError:
        import sys
        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "erro %s", ("supersecreto99",), sys.exc_info())
    out = fmt.format(rec)
    assert "supersecreto99" not in out and TOKEN not in out


def test_rate_limiter_window():
    now = [0.0]
    rl = RateLimiter(2, 10, clock=lambda: now[0])
    assert rl.allow("a")[0] and rl.allow("a")[0]
    ok, wait = rl.allow("a")
    assert not ok and 0 < wait <= 10
    assert rl.allow("b")[0]  # chaves independentes
    now[0] = 10.1
    assert rl.allow("a")[0]


def test_sanitize_user_text():
    assert sanitize_user_text(" oi\x00\x07 mundo ", 100) == "oi mundo"
    with pytest.raises(InputError) as e:
        sanitize_user_text("   ", 10)
    assert e.value.code == "empty"
    with pytest.raises(InputError) as e:
        sanitize_user_text("x" * 11, 10)
    assert e.value.code == "too_long"


def test_callback_validation():
    assert parse_callback("adm:users:3") == ["adm", "users", "3"]
    for bad in (None, "", "ADM:x", "adm:" + "x" * 70, "adm:../etc", "adm:a b", "adm;drop", "menu:" + "a:" * 6):
        with pytest.raises(InvalidCallback):
            parse_callback(bad)


def test_config_valid_and_error_does_not_leak_values():
    cfg = Config.from_env(make_env())
    assert cfg.owner_id == OWNER and cfg.free_message_limit == 10 and cfg.group_min_members == 500
    assert cfg.model_for("groq") == "modelo-teste" and cfg.model_for("xai") == "modelo-teste"
    assert TOKEN not in repr(cfg) and "gsk_testkey" not in repr(cfg)
    bad_token = "999999:segredo-invalido"
    with pytest.raises(ConfigError) as e:
        Config.from_env(make_env({"TELEGRAM_BOT_TOKEN": bad_token}))
    assert e.value.variable == "TELEGRAM_BOT_TOKEN" and "segredo-invalido" not in str(e.value)


def test_config_validation_rules():
    cases = [
        ({"OWNER_TELEGRAM_ID": ""}, "OWNER_TELEGRAM_ID"),
        ({"OWNER_TELEGRAM_ID": "abc"}, "OWNER_TELEGRAM_ID"),
        ({"ADMIN_TELEGRAM_IDS": "2,3"}, "ADMIN_TELEGRAM_IDS"),  # >2 admins no total
        ({"DEFAULT_PROVIDER": "naoexiste"}, "DEFAULT_PROVIDER"),
        ({"FALLBACK_PROVIDER": "groq"}, "FALLBACK_PROVIDER"),
        ({"FREE_MESSAGE_LIMIT": "-1"}, "FREE_MESSAGE_LIMIT"),
        ({"API_TIMEOUT_SECONDS": "x"}, "API_TIMEOUT_SECONDS"),
        ({"LOG_LEVEL": "VERBOSE"}, "LOG_LEVEL"),
        ({"GROQ_API_KEY": "tem espaco"}, "GROQ_API_KEY"),
        ({"SUPPORT_USERNAME": "a"}, "SUPPORT_USERNAME"),
        ({"GROUP_PREMIUM_FEATURE_ENABLED": "talvez"}, "GROUP_PREMIUM_FEATURE_ENABLED"),
    ]
    for extra, var in cases:
        with pytest.raises(ConfigError) as e:
            Config.from_env(make_env(extra))
        assert e.value.variable == var, (extra, e.value.variable)
    assert Config.from_env(make_env({"ADMIN_TELEGRAM_IDS": "2002"})).admin_ids == (2002,)
    assert Config.from_env(make_env({"SUPPORT_USERNAME": "@meu_suporte"})).support_username == "meu_suporte"


def test_support_forward_failure_handling_and_redaction(tmp_path):
    from tests.helpers import make_services, run
    from support import SupportService
    s = make_services(tmp_path)
    s.admin.add_admin(OWNER, 2002)
    sent = []

    async def send(chat_id, text):
        if chat_id == 2002:
            raise RuntimeError("admin não iniciou o bot")
        sent.append(text)

    sup = SupportService(s.config, s.db, send)
    res = run(sup.forward_to_admins(5, "ana", "ajuda! minha chave gsk_testkey1234567890abcd"))
    assert res.delivered == 1 and res.failed == 1 and res.ok
    assert "gsk_testkey1234567890abcd" not in sent[0] and "ana" in sent[0]
    assert "Nunca envie senhas" in sup.contact_text()


def test_keyboards_callbacks_are_valid():
    import keyboards as kb
    rows = kb.main_menu() + kb.admin_panel() + kb.clear_confirm() + kb.confirm("abcdef123456") + kb.pagination("adm:users", 1, 50, 10)
    for row in rows:
        for b in row:
            assert b.callback is not None
            parse_callback(b.callback)
