"""Suporte: contato configurável e encaminhamento opcional (e explícito) aos administradores."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from config import Config
from database import Database
from security import redact_secrets

log = logging.getLogger(__name__)
MAX_SUPPORT_CHARS = 1000


@dataclass(frozen=True)
class ForwardResult:
    delivered: int
    failed: int

    @property
    def ok(self) -> bool:
        return self.delivered > 0


class SupportService:
    def __init__(self, config: Config, db: Database, send: Callable[[int, str], Awaitable[None]]) -> None:
        self.config, self.db, self._send = config, db, send

    def contact_text(self) -> str:
        lines = ["Precisa de ajuda? Veja como falar com o suporte:"]
        if self.config.support_username:
            lines.append(f"• Telegram: @{self.config.support_username}")
        if self.config.support_channel:
            lines.append(f"• Canal: {self.config.support_channel}")
        note = self.db.get_setting("support_note", "")
        if note:
            lines.append(note)
        if self.config.support_forward_to_admins:
            lines.append("• Ou use /suporte para enviar uma mensagem diretamente à equipe.")
        if len(lines) == 1:
            lines.append("Nenhum contato foi configurado ainda. Avise o administrador do bot.")
        lines.append("Nunca envie senhas, tokens ou dados sensíveis.")
        return "\n".join(lines)

    async def forward_to_admins(self, user_id: int, username: str | None, text: str) -> ForwardResult:
        """Encaminha SOMENTE o texto que o usuário escolheu enviar pelo fluxo /suporte."""
        if not self.config.support_forward_to_admins:
            return ForwardResult(0, 0)
        body = redact_secrets(text.strip()[:MAX_SUPPORT_CHARS], self.config.secret_values())
        msg = f"[Suporte] de {user_id}" + (f" (@{username})" if username else "") + f":\n{body}"
        delivered = failed = 0
        for admin in self.db.list_admins():
            try:
                await self._send(admin["user_id"], msg)
                delivered += 1
            except Exception:  # noqa: BLE001 - admin pode não ter iniciado o bot
                failed += 1
                log.warning("falha ao encaminhar suporte a um administrador")
        self.db.add_audit(user_id, "support_forward", None, delivered > 0, f"entregues={delivered} falhas={failed}")
        return ForwardResult(delivered, failed)
