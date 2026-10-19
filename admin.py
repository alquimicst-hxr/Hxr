"""Administração: autorização no servidor, ações auditadas e confirmações.

Identidade = ID verificado pelo Telegram + lista de permissões persistente.
Não há senhas. Toda operação revalida a permissão do ator.
"""
from __future__ import annotations

import secrets
import time
from typing import Any

from config import MAX_ADMINS, PAID_PLANS, Config
from database import Database
from security import PermissionDenied, parse_user_id, require
from subscriptions import SubscriptionService

PAGE_SIZE = 10
ALLOWED_SETTINGS = {"maintenance": "0 ou 1", "group_min_members": "inteiro 1..10000000", "support_note": "texto até 300 caracteres"}


class AdminError(Exception):
    """Erro de regra de negócio, seguro para exibir ao administrador."""


class ConfirmationStore:
    """Confirmações de operações destrutivas: token aleatório, vinculado ao ator, expira."""

    def __init__(self, ttl: float = 120.0, clock=time.monotonic) -> None:
        self.ttl, self.clock = ttl, clock
        self._items: dict[str, tuple[int, str, tuple, float]] = {}

    def create(self, actor: int, action: str, args: tuple) -> str:
        now = self.clock()
        self._items = {k: v for k, v in self._items.items() if v[3] > now}
        token = secrets.token_hex(6)
        self._items[token] = (actor, action, args, now + self.ttl)
        return token

    def consume(self, token: str, actor: int) -> tuple[str, tuple]:
        item = self._items.get(token)
        if item is None or item[3] <= self.clock():
            self._items.pop(token, None)
            raise AdminError("Confirmação expirada ou inexistente.")
        if item[0] != actor:
            raise PermissionDenied("confirmação pertence a outro administrador")
        del self._items[token]
        return item[1], item[2]

    def cancel(self, token: str, actor: int) -> None:
        item = self._items.get(token)
        if item and item[0] == actor:
            del self._items[token]


class AdminService:
    def __init__(self, db: Database, config: Config, subs: SubscriptionService) -> None:
        self.db, self.config, self.subs = db, config, subs
        self.confirmations = ConfirmationStore()

    # ---- identidade ----------------------------------------------------
    def bootstrap(self) -> None:
        self.db.add_admin(self.config.owner_id, "owner", None)
        for uid in self.config.admin_ids:
            if self.db.get_admin(uid) is None and len(self.db.list_admins()) < MAX_ADMINS:
                self.db.add_admin(uid, "admin", self.config.owner_id)

    def is_owner(self, user_id: int) -> bool:
        return user_id == self.config.owner_id

    def is_admin(self, user_id: int) -> bool:
        return self.is_owner(user_id) or self.db.get_admin(user_id) is not None

    def require_admin(self, actor: int) -> None:
        require(self.is_admin(actor), "somente administradores")

    def require_owner(self, actor: int) -> None:
        require(self.is_owner(actor), "somente o proprietário")

    def _log(self, actor: int, action: str, target: Any = "", detail: str = "") -> None:
        self.db.add_admin_event(actor, action, str(target), detail)
        self.db.add_audit(actor, f"admin:{action}", None, True, str(target))

    # ---- administradores -----------------------------------------------
    def add_admin(self, actor: int, target: int) -> None:
        self.require_owner(actor)
        target = parse_user_id(target)
        if self.is_admin(target):
            raise AdminError("Esse usuário já é administrador.")
        if len(self.db.list_admins()) >= MAX_ADMINS:
            raise AdminError(f"Limite de {MAX_ADMINS} administradores (incluindo o proprietário) atingido.")
        self.db.add_admin(target, "admin", actor)
        self._log(actor, "add_admin", target)

    def remove_admin(self, actor: int, target: int) -> None:
        self.require_owner(actor)
        if self.is_owner(target):
            raise AdminError("O proprietário não pode ser removido.")
        if not self.db.remove_admin(target):
            raise AdminError("Esse usuário não é administrador.")
        self._log(actor, "remove_admin", target)

    # ---- assinaturas ---------------------------------------------------
    def grant(self, actor: int, target: int, plan: str, days: int) -> int:
        self.require_admin(actor)
        target = int(target)
        if target == 0:
            raise AdminError("ID inválido.")
        if plan not in PAID_PLANS:
            raise AdminError("Plano inválido. Use: " + ", ".join(PAID_PLANS))
        sub_id = self.subs.grant(target, plan, days, actor)
        self._log(actor, "grant", target, f"{plan} {days}d")
        return sub_id

    def renew(self, actor: int, target: int, days: int) -> None:
        self.require_admin(actor)
        self.subs.renew(int(target), days, actor)
        self._log(actor, "renew", target, f"+{days}d")

    def revoke(self, actor: int, target: int) -> bool:
        self.require_admin(actor)
        done = self.subs.cancel(int(target), actor)
        self._log(actor, "revoke", target, "ok" if done else "sem assinatura")
        return done

    # ---- bloqueios -----------------------------------------------------
    def block(self, actor: int, target: int, reason: str = "") -> None:
        self.require_admin(actor)
        target = parse_user_id(target)
        if self.is_admin(target):
            raise AdminError("Não é possível bloquear administradores.")
        self.db.block_user(target, reason, actor)
        self._log(actor, "block", target, reason[:100])

    def unblock(self, actor: int, target: int) -> bool:
        self.require_admin(actor)
        done = self.db.unblock_user(parse_user_id(target))
        self._log(actor, "unblock", target)
        return done

    # ---- consultas -----------------------------------------------------
    def stats(self, actor: int) -> dict:
        self.require_admin(actor)
        return self.db.stats()

    def list_users(self, actor: int, page: int) -> tuple[list[dict], int]:
        self.require_admin(actor)
        page = max(page, 0)
        rows = self.db.list_users(PAGE_SIZE, page * PAGE_SIZE)
        out = [{"user_id": r["user_id"], "username": r["username"], "plan": self.subs.peek(r["user_id"]).plan,
                "blocked": self.db.is_blocked(r["user_id"])} for r in rows]
        return out, self.db.count_users()

    def list_events(self, actor: int, page: int):
        self.require_admin(actor)
        page = max(page, 0)
        return self.db.list_admin_events(PAGE_SIZE, page * PAGE_SIZE), self.db.count_admin_events()

    # ---- configurações permitidas --------------------------------------
    def set_setting(self, actor: int, key: str, value: str) -> None:
        self.require_admin(actor)
        if key not in ALLOWED_SETTINGS:
            raise AdminError("Configuração não permitida. Disponíveis: " + ", ".join(ALLOWED_SETTINGS))
        value = value.strip()
        if key == "maintenance" and value not in ("0", "1"):
            raise AdminError("maintenance aceita 0 ou 1.")
        if key == "group_min_members":
            if not value.isdigit() or not 1 <= int(value) <= 10_000_000:
                raise AdminError("group_min_members deve ser inteiro entre 1 e 10000000.")
        if key == "support_note" and len(value) > 300:
            raise AdminError("support_note aceita até 300 caracteres.")
        self.db.set_setting(key, value)
        self._log(actor, "set_setting", key, value[:60] if key != "support_note" else "")

    def get_settings(self, actor: int) -> dict:
        self.require_admin(actor)
        return {k: self.db.get_setting(k, "") for k in ALLOWED_SETTINGS}

    # ---- grupos --------------------------------------------------------
    def set_group_enabled(self, actor: int, chat_id: int, enabled: bool, actor_is_group_admin: bool = False) -> None:
        require(self.is_admin(actor) or actor_is_group_admin, "somente administradores do grupo ou do bot")
        if not self.db.set_group_field(chat_id, "enabled", 1 if enabled else 0):
            raise AdminError("Grupo não registrado.")
        self._log(actor, "group_enable" if enabled else "group_disable", chat_id)

    def set_group_default_topic(self, actor: int, chat_id: int, thread_id: int | None, actor_is_group_admin: bool = False) -> None:
        require(self.is_admin(actor) or actor_is_group_admin, "somente administradores do grupo ou do bot")
        if not self.db.set_group_field(chat_id, "default_thread_id", thread_id):
            raise AdminError("Grupo não registrado.")
        self._log(actor, "group_topic", chat_id, str(thread_id))

    def set_group_premium(self, actor: int, chat_id: int, enabled: bool) -> None:
        self.require_admin(actor)  # só administradores do bot: afeta cobrança
        if not self.db.set_group_field(chat_id, "premium_enabled", 1 if enabled else 0):
            raise AdminError("Grupo não registrado.")
        self._log(actor, "group_premium", chat_id, str(enabled))
