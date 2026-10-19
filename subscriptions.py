"""Planos, cotas e assinaturas.

Política de consumo (documentada e testada):
  * 1 mensagem = 1 geração de IA concluída com sucesso.
  * A cota é RESERVADA (atomicamente, BEGIN IMMEDIATE) antes de chamar o
    provedor e DEVOLVIDA (refund) se a geração falhar (timeout, erro do
    provedor, erro interno). Entradas inválidas, bloqueios e negações por
    cota nunca consomem.
  * Se a geração der certo mas o envio ao Telegram falhar, a cota continua
    consumida (o custo já ocorreu).
  * Gratuito: contador vitalício ('lifetime'); /start, /reset e reinício do
    bot não o zeram. Pagos: contador por período da assinatura.
  * Assinaturas pagas só são ativadas por administrador (source='admin') ou
    após confirmação de um PaymentVerifier (source='payment').
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Protocol

from config import PAID_PLANS
from database import Database

PLAN_RANK = {"free": 0, "standard": 1, "pro": 2, "ultra": 3}


class SubscriptionError(Exception):
    pass


class QuotaExceeded(SubscriptionError):
    def __init__(self, plan: str, quota: int) -> None:
        super().__init__(f"cota esgotada no plano {plan}")
        self.plan, self.quota = plan, quota


class PaymentNotConfigured(SubscriptionError):
    pass


class PaymentNotVerified(SubscriptionError):
    pass


@dataclass(frozen=True)
class Reservation:
    subject_id: int
    plan: str
    period_key: str
    used: int
    quota: int
    model: str
    expires_at: Optional[float]

    @property
    def remaining(self) -> int:
        return max(self.quota - self.used, 0)


@dataclass(frozen=True)
class VerifiedPayment:
    """Só deve ser criado por um PaymentVerifier após confirmação real."""
    subject_id: int
    plan: str
    days: int
    reference: str


class PaymentVerifier(Protocol):
    def verify(self, payload: dict) -> Optional[VerifiedPayment]: ...


class SubscriptionService:
    def __init__(self, db: Database, clock=time.time, payment_verifier: Optional[PaymentVerifier] = None) -> None:
        self.db, self.clock, self.payment_verifier = db, clock, payment_verifier

    # ---- internos ------------------------------------------------------
    def _expire(self, conn, subject_id: int, now: float) -> None:
        for row in conn.execute("SELECT id,plan FROM subscriptions WHERE subject_id=? AND status='active' AND expires_at<=?",
                                (subject_id, now)).fetchall():
            conn.execute("UPDATE subscriptions SET status='expired' WHERE id=?", (row["id"],))
            conn.execute("INSERT INTO subscription_events(subject_id,action,plan,actor,detail,ts) VALUES(?,?,?,?,?,?)",
                         (subject_id, "expired", row["plan"], None, "vencimento automático", now))

    @staticmethod
    def _active(conn, subject_id: int, now: float):
        rows = conn.execute("SELECT s.*, p.quota, p.model FROM subscriptions s JOIN plans p ON p.name=s.plan "
                            "WHERE s.subject_id=? AND s.status='active' AND s.expires_at>?", (subject_id, now)).fetchall()
        return max(rows, key=lambda r: (PLAN_RANK.get(r["plan"], 0), r["expires_at"]), default=None)

    @staticmethod
    def _event(conn, subject_id, action, plan, actor, detail, now) -> None:
        conn.execute("INSERT INTO subscription_events(subject_id,action,plan,actor,detail,ts) VALUES(?,?,?,?,?,?)",
                     (subject_id, action, plan, actor, (detail or "")[:200], now))

    # ---- consulta ------------------------------------------------------
    def peek(self, subject_id: int, now: float | None = None) -> Reservation:
        """Situação atual sem alterar nada (nem expirar)."""
        now = self.clock() if now is None else now
        with self.db.connect() as conn:
            return self._reservation(conn, subject_id, now, bump=0)

    def _reservation(self, conn, subject_id: int, now: float, bump: int) -> Reservation:
        sub = self._active(conn, subject_id, now)
        if sub:
            idx = int((now - sub["started_at"]) // (sub["period_days"] * 86400))
            key, plan, quota, model, exp = f"s{sub['id']}:{idx}", sub["plan"], sub["quota"], sub["model"], sub["expires_at"]
        else:
            p = conn.execute("SELECT quota,model FROM plans WHERE name='free'").fetchone()
            if p is None:
                raise SubscriptionError("planos não inicializados (execute sync_plans)")
            key, plan, quota, model, exp = "lifetime", "free", p["quota"], p["model"], None
        row = conn.execute("SELECT used FROM usage_counters WHERE subject_id=? AND period_key=?", (subject_id, key)).fetchone()
        return Reservation(subject_id, plan, key, (row["used"] if row else 0) + bump, quota, model or "", exp)

    def has_active_paid(self, subject_id: int) -> bool:
        return self.peek(subject_id).plan != "free"

    def usage_status(self, subject_id: int) -> Reservation:
        return self.peek(subject_id)

    # ---- consumo -------------------------------------------------------
    def reserve(self, subject_id: int) -> Reservation:
        """Verifica expiração e reserva 1 mensagem de forma atômica."""
        now = self.clock()
        denied: Optional[QuotaExceeded] = None
        with self.db.transaction() as conn:
            self._expire(conn, subject_id, now)
            res = self._reservation(conn, subject_id, now, bump=0)
            if res.used >= res.quota:
                denied = QuotaExceeded(res.plan, res.quota)
            else:
                conn.execute("INSERT INTO usage_counters(subject_id,period_key,used) VALUES(?,?,1) "
                             "ON CONFLICT(subject_id,period_key) DO UPDATE SET used=used+1", (subject_id, res.period_key))
                res = Reservation(res.subject_id, res.plan, res.period_key, res.used + 1, res.quota, res.model, res.expires_at)
        if denied:
            raise denied
        return res

    def refund(self, res: Reservation) -> None:
        """Devolve uma reserva (falha recuperável)."""
        with self.db.transaction() as conn:
            conn.execute("UPDATE usage_counters SET used=MAX(used-1,0) WHERE subject_id=? AND period_key=?",
                         (res.subject_id, res.period_key))

    # ---- administração de assinaturas ----------------------------------
    def grant(self, subject_id: int, plan: str, days: int, actor: int | None, source: str = "admin") -> int:
        if plan not in PAID_PLANS:
            raise SubscriptionError("plano inválido")
        if not 1 <= days <= 3650:
            raise SubscriptionError("duração inválida (1 a 3650 dias)")
        if source not in ("admin", "payment"):
            raise SubscriptionError("origem inválida")
        now = self.clock()
        with self.db.transaction() as conn:
            p = conn.execute("SELECT period_days FROM plans WHERE name=?", (plan,)).fetchone()
            if p is None:
                raise SubscriptionError("planos não inicializados")
            self._expire(conn, subject_id, now)
            for old in conn.execute("SELECT id FROM subscriptions WHERE subject_id=? AND status='active'", (subject_id,)).fetchall():
                conn.execute("UPDATE subscriptions SET status='replaced' WHERE id=?", (old["id"],))
            cur = conn.execute("INSERT INTO subscriptions(subject_id,plan,started_at,expires_at,period_days,status,granted_by,source) "
                               "VALUES(?,?,?,?,?,'active',?,?)", (subject_id, plan, now, now + days * 86400, p["period_days"], actor, source))
            self._event(conn, subject_id, "granted", plan, actor, f"{days} dias via {source}", now)
            return cur.lastrowid

    def renew(self, subject_id: int, days: int, actor: int | None) -> float:
        if not 1 <= days <= 3650:
            raise SubscriptionError("duração inválida (1 a 3650 dias)")
        now = self.clock()
        with self.db.transaction() as conn:
            self._expire(conn, subject_id, now)
            sub = self._active(conn, subject_id, now)
            if sub is None:
                raise SubscriptionError("sem assinatura ativa para renovar")
            new_exp = sub["expires_at"] + days * 86400
            conn.execute("UPDATE subscriptions SET expires_at=? WHERE id=?", (new_exp, sub["id"]))
            self._event(conn, subject_id, "renewed", sub["plan"], actor, f"+{days} dias", now)
            return new_exp

    def cancel(self, subject_id: int, actor: int | None) -> bool:
        now = self.clock()
        with self.db.transaction() as conn:
            self._expire(conn, subject_id, now)
            sub = self._active(conn, subject_id, now)
            if sub is None:
                return False
            conn.execute("UPDATE subscriptions SET status='cancelled' WHERE id=?", (sub["id"],))
            self._event(conn, subject_id, "cancelled", sub["plan"], actor, "", now)
            return True

    def history(self, subject_id: int, limit: int = 20):
        return self.db._all("SELECT * FROM subscription_events WHERE subject_id=? ORDER BY id DESC LIMIT ?", (subject_id, limit))

    # ---- pagamentos (preparação) ---------------------------------------
    def activate_from_payment(self, payload: dict) -> int:
        """Ativa plano pago SOMENTE após um PaymentVerifier confirmar.
        Sem verificador configurado, nada é ativado."""
        if self.payment_verifier is None:
            raise PaymentNotConfigured("nenhum provedor de pagamento configurado")
        verified = self.payment_verifier.verify(payload)
        if not isinstance(verified, VerifiedPayment):
            raise PaymentNotVerified("pagamento não confirmado")
        return self.grant(verified.subject_id, verified.plan, verified.days, None, source="payment")
