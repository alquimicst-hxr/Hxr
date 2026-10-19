"""Orquestração de geração: validação, limites, cota, histórico e provedores."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import prompts
from api_providers import ProviderError, ProviderRouter
from config import Config
from database import Database
from groups import GroupService
from security import InputError, RateLimiter, redact_secrets, sanitize_user_text
from subscriptions import QuotaExceeded, Reservation, SubscriptionService

log = logging.getLogger(__name__)

USER_MESSAGES = {
    "auth": "O serviço de IA está temporariamente indisponível. Tente novamente mais tarde.",
    "config": "O serviço de IA ainda não está configurado corretamente. Avise o suporte.",
    "unavailable": "O serviço de IA está indisponível no momento. Tente novamente em instantes.",
    "timeout": "A IA demorou demais para responder. Sua mensagem não foi descontada; tente novamente.",
    "rate_limit": "O serviço de IA está com muitas requisições. Tente novamente em instantes.",
    "bad_request": "Não consegui processar esse pedido. Tente reformular a mensagem.",
    "bad_response": "Recebi uma resposta inválida do serviço de IA. Tente novamente.",
}


class AIError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class UserBlocked(AIError):
    pass


class MaintenanceMode(AIError):
    pass


class RateLimited(AIError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Muitas mensagens seguidas. Aguarde {int(retry_after) + 1}s.")
        self.retry_after = retry_after


class GenerationFailed(AIError):
    def __init__(self, kind: str) -> None:
        super().__init__(USER_MESSAGES.get(kind, USER_MESSAGES["unavailable"]))
        self.kind = kind


@dataclass(frozen=True)
class GenerationResult:
    text: str
    reservation: Reservation
    provider: str
    reported_model: Optional[str]


class AIService:
    def __init__(self, config: Config, db: Database, subs: SubscriptionService, router: ProviderRouter,
                 groups: GroupService, is_admin=lambda uid: False, limiter: RateLimiter | None = None) -> None:
        self.config, self.db, self.subs, self.router, self.groups = config, db, subs, router, groups
        self.is_admin = is_admin
        self.limiter = limiter or RateLimiter(config.rate_limit_messages, config.rate_limit_window)
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

    def _lock(self, key: tuple[int, int]) -> asyncio.Lock:
        if len(self._locks) > 5000:
            self._locks = {k: v for k, v in self._locks.items() if v.locked()}
        return self._locks.setdefault(key, asyncio.Lock())

    async def generate(self, user_id: int, chat_id: int, thread_id: int, text: str, is_group: bool = False) -> GenerationResult:
        """Função central. Ordem: validar -> bloqueio/manutenção -> frequência
        -> reservar cota -> chamar provedor -> gravar histórico (ou devolver cota)."""
        try:
            clean = sanitize_user_text(text, self.config.max_input_chars)
        except InputError as err:
            raise AIError("Envie uma mensagem de texto." if err.code == "empty"
                          else f"Mensagem muito longa (máximo {self.config.max_input_chars} caracteres).") from None
        if await asyncio.to_thread(self.db.is_blocked, user_id):
            raise UserBlocked("Seu acesso está bloqueado. Fale com o suporte se achar que é um engano.")
        if not self.is_admin(user_id) and await asyncio.to_thread(self.db.get_setting, "maintenance", "0") == "1":
            raise MaintenanceMode("O bot está em manutenção. Tente novamente mais tarde.")
        ok, wait = self.limiter.allow(user_id)
        if not ok:
            raise RateLimited(wait)

        subject = await asyncio.to_thread(self.groups.billing_subject, user_id, chat_id, is_group, self.subs)
        reservation = await asyncio.to_thread(self.subs.reserve, subject)  # pode levantar QuotaExceeded

        try:
            async with self._lock((chat_id, thread_id)):
                rows = await asyncio.to_thread(self.db.recent_messages, chat_id, thread_id, self.config.history_max_messages)
                history = [{"role": r["role"], "content": r["content"]} for r in rows]
                custom = "" if is_group else await asyncio.to_thread(self.db.get_custom_instructions, user_id)
                system = prompts.build_system_prompt(self.config.default_personality, custom, is_group)
                messages = prompts.build_messages(system, history, clean, self.config.history_max_chars)
                secrets = self.config.secret_values()  # nunca enviar segredos aos provedores
                for m in messages:
                    m["content"] = redact_secrets(m["content"], secrets)
                completion = await self.router.generate(messages, self.config.max_output_tokens, reservation.model)
                await asyncio.to_thread(self.db.add_messages, chat_id, thread_id, user_id,
                                        [("user", clean), ("assistant", completion.text)])
                await asyncio.to_thread(self.db.prune_history, chat_id, thread_id, self.config.history_max_messages)
        except ProviderError as err:
            await asyncio.to_thread(self.subs.refund, reservation)
            raise GenerationFailed(err.kind) from None
        except BaseException:
            await asyncio.to_thread(self.subs.refund, reservation)
            raise
        return GenerationResult(completion.text, reservation, completion.provider, completion.reported_model)

    async def clear_history(self, chat_id: int, thread_id: int) -> int:
        return await asyncio.to_thread(self.db.clear_history, chat_id, thread_id)

    def purge_expired_history(self) -> int:
        return self.db.purge_older_than(self.config.history_retention_days)
