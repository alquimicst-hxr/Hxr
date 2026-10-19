import threading

import pytest

from subscriptions import PaymentNotConfigured, PaymentNotVerified, QuotaExceeded, SubscriptionError, SubscriptionService, VerifiedPayment
from tests.helpers import make_services

DAY = 86400


def test_free_limit_is_persistent_and_blocks(tmp_path):
    s = make_services(tmp_path, {"FREE_MESSAGE_LIMIT": "3"})
    for i in range(3):
        r = s.subs.reserve(42)
        assert r.plan == "free" and r.remaining == 2 - i
    with pytest.raises(QuotaExceeded):
        s.subs.reserve(42)
    # "reinício do bot": novos objetos sobre o mesmo banco não zeram a cota
    s2 = SubscriptionService(s.db, clock=s.clock)
    with pytest.raises(QuotaExceeded):
        s2.reserve(42)


def test_refund_returns_quota(tmp_path):
    s = make_services(tmp_path, {"FREE_MESSAGE_LIMIT": "1"})
    r = s.subs.reserve(1)
    s.subs.refund(r)
    assert s.subs.reserve(1).remaining == 0


def test_concurrent_reservations_never_exceed_quota(tmp_path):
    s = make_services(tmp_path, {"FREE_MESSAGE_LIMIT": "5"})
    ok, denied = [], []

    def work():
        try:
            s.subs.reserve(99)
            ok.append(1)
        except QuotaExceeded:
            denied.append(1)

    threads = [threading.Thread(target=work) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 5 and len(denied) == 15


def test_grant_expire_and_fall_back_to_free(tmp_path):
    s = make_services(tmp_path, {"FREE_MESSAGE_LIMIT": "2", "STANDARD_QUOTA": "100"})
    s.subs.grant(7, "standard", 10, actor=1)
    assert s.subs.reserve(7).plan == "standard"
    s.clock.t += 11 * DAY  # vencida
    r = s.subs.reserve(7)
    assert r.plan == "free"
    actions = [e["action"] for e in s.subs.history(7)]
    assert "expired" in actions and "granted" in actions


def test_paid_quota_resets_each_period(tmp_path):
    s = make_services(tmp_path, {"STANDARD_QUOTA": "2", "PLAN_PERIOD_DAYS": "30"})
    s.subs.grant(8, "standard", 90, actor=1)
    s.subs.reserve(8)
    s.subs.reserve(8)
    with pytest.raises(QuotaExceeded):
        s.subs.reserve(8)
    s.clock.t += 31 * DAY
    assert s.subs.reserve(8).used == 1


def test_renew_cancel_and_history(tmp_path):
    s = make_services(tmp_path)
    s.subs.grant(9, "pro", 5, actor=1)
    new_exp = s.subs.renew(9, 5, actor=1)
    assert new_exp == s.clock.t + 10 * DAY
    assert s.subs.cancel(9, actor=1) is True
    assert s.subs.cancel(9, actor=1) is False
    assert s.subs.peek(9).plan == "free"
    assert [e["action"] for e in s.subs.history(9)][:2] == ["cancelled", "renewed"]


def test_grant_validation(tmp_path):
    s = make_services(tmp_path)
    with pytest.raises(SubscriptionError):
        s.subs.grant(1, "free", 5, actor=1)
    with pytest.raises(SubscriptionError):
        s.subs.grant(1, "pro", 0, actor=1)


def test_payment_activation_requires_verifier(tmp_path):
    s = make_services(tmp_path)
    with pytest.raises(PaymentNotConfigured):
        s.subs.activate_from_payment({"plan": "pro"})

    class Rejecting:
        def verify(self, payload):
            return None

    s.subs.payment_verifier = Rejecting()
    with pytest.raises(PaymentNotVerified):
        s.subs.activate_from_payment({"plan": "pro"})
    assert s.subs.peek(5).plan == "free"

    class Accepting:
        def verify(self, payload):
            return VerifiedPayment(5, "pro", 30, "ref-1")

    s.subs.payment_verifier = Accepting()
    s.subs.activate_from_payment({})
    assert s.subs.peek(5).plan == "pro"


def test_admin_permissions(tmp_path):
    from admin import AdminError
    from security import PermissionDenied
    s = make_services(tmp_path)
    owner = s.config.owner_id
    with pytest.raises(PermissionDenied):
        s.admin.grant(555, 7, "pro", 5)  # não-admin
    s.admin.grant(owner, 7, "pro", 5)
    assert s.subs.peek(7).plan == "pro"
    with pytest.raises(PermissionDenied):
        s.admin.add_admin(555, 555)  # auto-promoção
    s.admin.add_admin(owner, 2002)
    with pytest.raises(AdminError):
        s.admin.add_admin(owner, 3003)  # limite de 2 admins no total
    with pytest.raises(PermissionDenied):
        s.admin.remove_admin(2002, owner)  # admin comum não remove
    with pytest.raises(AdminError):
        s.admin.remove_admin(owner, owner)  # dono não pode ser removido
    with pytest.raises(AdminError):
        s.admin.block(owner, 2002)  # não bloqueia admin
    s.admin.remove_admin(owner, 2002)
    with pytest.raises(PermissionDenied):
        s.admin.grant(2002, 7, "pro", 5)  # perdeu a permissão


def test_confirmation_is_bound_to_actor_and_expires(tmp_path):
    from admin import AdminError, ConfirmationStore
    from security import PermissionDenied
    now = [0.0]
    store = ConfirmationStore(ttl=10, clock=lambda: now[0])
    tok = store.create(1, "revoke", (5,))
    with pytest.raises(PermissionDenied):
        store.consume(tok, 2)
    assert store.consume(tok, 1) == ("revoke", (5,))
    with pytest.raises(AdminError):
        store.consume(tok, 1)  # uso único
    tok2 = store.create(1, "revoke", (5,))
    now[0] = 11
    with pytest.raises(AdminError):
        store.consume(tok2, 1)


def test_groups_and_topics(tmp_path):
    s = make_services(tmp_path, {"GROUP_PREMIUM_FEATURE_ENABLED": "true", "GROUP_MIN_MEMBERS": "500"})
    g, a, owner = s.groups, s.admin, s.config.owner_id
    g.register(-100, "G")
    assert not g.can_respond(-100, 0)  # desativado por padrão
    a.set_group_enabled(owner, -100, True)
    assert g.can_respond(-100, 0) and g.can_respond(-100, 7)
    a.set_group_default_topic(owner, -100, 7)
    assert g.can_respond(-100, 7) and not g.can_respond(-100, 8)
    g.enable_topic(-100, 8, True)
    assert g.can_respond(-100, 8)
    g.enable_topic(-100, 7, False)
    assert not g.can_respond(-100, 7)
    from security import PermissionDenied
    with pytest.raises(PermissionDenied):
        a.set_group_enabled(555, -100, False)  # nem admin do bot nem do grupo
    a.set_group_enabled(555, -100, False, actor_is_group_admin=True)

    # elegibilidade premium: nunca automática
    async def fetch_ok(_):
        return 800

    async def fetch_fail(_):
        raise RuntimeError("x")

    from tests.helpers import run
    assert run(g.refresh_member_count(-100, fetch_ok)) == 800
    assert not g.premium_eligible(-100)  # falta ativar o recurso no grupo
    a.set_group_premium(owner, -100, True)
    assert g.premium_eligible(-100)
    assert run(g.refresh_member_count(-100, fetch_fail)) is None
    assert not g.premium_eligible(-100)  # desconhecido => sem benefício
