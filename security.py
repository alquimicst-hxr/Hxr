"""Segurança: redação de segredos, limitação de frequência, validação de
entradas e callbacks.

Aviso: código Python distribuído não pode ser considerado inviolável
apenas por ofuscação ou criptografia; a proteção real vem de manter
segredos fora do código/repositório e validar tudo no servidor.
"""
from __future__ import annotations

import logging
import re
import time
from collections import deque
from typing import Callable, Iterable

REDACTED = "[REDACTED]"
_PATTERNS = [
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),  # token de bot do Telegram
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\b(?:sk|gsk|xai|nvapi|hf|fw|csk|pplx)[-_][A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|senha)\s*[=:]\s*\S+"),
]
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CALLBACK_RE = re.compile(r"^[a-z_]{1,16}(:[A-Za-z0-9_\-]{1,32}){0,4}$")


class SecurityError(Exception):
    """Base para erros de segurança."""


class PermissionDenied(SecurityError):
    pass


class InputError(SecurityError):
    """Entrada inválida; `code` é 'empty' ou 'too_long'."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InvalidCallback(SecurityError):
    pass


def redact_secrets(text: str, extra: Iterable[str] = ()) -> str:
    """Remove padrões conhecidos de segredos e valores exatos em `extra`."""
    if not text:
        return text
    out = str(text)
    for secret in extra:
        if secret and len(secret) >= 6:
            out = out.replace(secret, REDACTED)
    for pat in _PATTERNS:
        if pat.groups:
            out = pat.sub(lambda m: f"{m.group(1)}={REDACTED}", out)
        else:
            out = pat.sub(REDACTED, out)
    return out


def mask_token(token: str) -> str:
    return (token[:4] + "…") if token and len(token) > 8 else REDACTED


class RedactingFormatter(logging.Formatter):
    """Formatter que redige segredos (inclui stack traces)."""

    def __init__(self, fmt: str | None = None, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt)
        self._secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record), self._secrets)


class RateLimiter:
    """Janela deslizante em memória: no máximo `max_events` por `window`s."""

    def __init__(self, max_events: int, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        if max_events < 1 or window <= 0:
            raise ValueError("parâmetros inválidos")
        self.max_events, self.window, self._clock = max_events, window, clock
        self._events: dict[object, deque] = {}

    def allow(self, key: object) -> tuple[bool, float]:
        """Registra uma tentativa. Retorna (permitido, segundos_para_tentar)."""
        now = self._clock()
        q = self._events.setdefault(key, deque())
        while q and now - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.max_events:
            return False, max(0.0, self.window - (now - q[0]))
        q.append(now)
        if len(self._events) > 10000:  # evita crescimento ilimitado
            for k in [k for k, v in self._events.items() if not v or now - v[-1] >= self.window]:
                self._events.pop(k, None)
        return True, 0.0


def sanitize_user_text(text: str | None, max_chars: int) -> str:
    """Remove caracteres de controle, normaliza e aplica limite de tamanho."""
    cleaned = _CTRL.sub("", text or "").replace("\r\n", "\n").strip()
    if not cleaned:
        raise InputError("empty")
    if len(cleaned) > max_chars:
        raise InputError("too_long")
    return cleaned


def parse_callback(data: str | None) -> list[str]:
    """Valida e divide `callback_data` ('acao:arg:arg'). Estrito."""
    if not data or len(data) > 64 or not _CALLBACK_RE.match(data):
        raise InvalidCallback("callback inválido")
    return data.split(":")


def parse_user_id(value: str | int) -> int:
    try:
        uid = int(str(value).strip())
    except (ValueError, TypeError):
        raise InputError("empty") from None
    if not 0 < uid < 2**52:
        raise InputError("empty")
    return uid


def parse_chat_id(value: str | int) -> int:
    try:
        cid = int(str(value).strip())
    except (ValueError, TypeError):
        raise InputError("empty") from None
    if cid == 0 or abs(cid) >= 2**53:
        raise InputError("empty")
    return cid


def require(condition: bool, message: str = "permissão negada") -> None:
    if not condition:
        raise PermissionDenied(message)


def safe_config_summary(config) -> dict:
    """Resumo da configuração sem nenhum segredo (para /status e logs)."""
    return {
        "provider": config.default_provider,
        "fallback": config.fallback_provider or None,
        "providers_configured": config.configured_providers(),
        "model_configured": bool(config.default_model or config.provider_models.get(config.default_provider)),
        "free_limit": config.free_message_limit,
        "log_level": config.log_level,
    }
