"""Grupos, supergrupos e tópicos de fórum."""
from __future__ import annotations

import time
from typing import Awaitable, Callable, Optional

from config import Config
from database import Database

MEMBER_COUNT_TTL = 24 * 3600


class GroupService:
    def __init__(self, db: Database, config: Config, clock=time.time) -> None:
        self.db, self.config, self.clock = db, config, clock

    def register(self, chat_id: int, title: str | None, thread_id: int = 0) -> None:
        self.db.upsert_group(chat_id, title, self.config.groups_enabled_by_default)

    def min_members(self) -> int:
        try:
            return int(self.db.get_setting("group_min_members", "") or self.config.group_min_members)
        except ValueError:
            return self.config.group_min_members

    def can_respond(self, chat_id: int, thread_id: int) -> bool:
        """Grupo ativo e tópico permitido. Uma regra explícita por tópico
        (enable_topic) tem prioridade; sem regra, vale o tópico padrão do
        grupo (se definido, só responde nele)."""
        g = self.db.get_group(chat_id)
        if g is None or not g["enabled"]:
            return False
        topic = self.db.get_topic(chat_id, thread_id) if thread_id else None
        if topic is not None:
            return bool(topic["enabled"])
        default = g["default_thread_id"]
        return default is None or thread_id == default

    def enable_topic(self, chat_id: int, thread_id: int, enabled: bool) -> None:
        self.db.upsert_topic(chat_id, thread_id, None, enabled)

    async def refresh_member_count(self, chat_id: int, fetch: Callable[[int], Awaitable[int]]) -> Optional[int]:
        """Consulta a contagem real via API do Telegram; falha => desconhecido (None)."""
        try:
            count = await fetch(chat_id)
            if not isinstance(count, int) or count < 0:
                raise ValueError
        except Exception:  # noqa: BLE001
            self.db.set_group_member_count(chat_id, None)
            return None
        self.db.set_group_member_count(chat_id, count)
        return count

    def premium_eligible(self, chat_id: int) -> bool:
        """Elegível = recurso habilitado na config + ativado para o grupo +
        contagem VERIFICADA recente >= mínimo. Nunca automático."""
        if not self.config.group_premium_feature_enabled:
            return False
        g = self.db.get_group(chat_id)
        if g is None or not g["premium_enabled"] or g["member_count"] is None or g["member_count_checked_at"] is None:
            return False
        if self.clock() - g["member_count_checked_at"] > MEMBER_COUNT_TTL:
            return False
        return g["member_count"] >= self.min_members()

    def billing_subject(self, user_id: int, chat_id: int, is_group: bool, subs) -> int:
        """Grupo elegível com assinatura ativa paga as mensagens; senão, o usuário."""
        if is_group and self.premium_eligible(chat_id) and subs.has_active_paid(chat_id):
            return chat_id
        return user_id
