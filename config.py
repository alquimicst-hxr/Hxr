"""Configuração centralizada do HaxorGPT Luna 7.1.

Lê variáveis de ambiente, valida tipos/limites e nunca inclui valores
secretos nas mensagens de erro (apenas o NOME da variável inválida).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional

VERSION = "7.1"

# provedor -> variável de ambiente com a chave de API
PROVIDER_KEY_VARS: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "together": "TOGETHER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "xai": "XAI_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "huggingface": "HUGGINGFACE_API_KEY",
    "inference_net": "INFERENCE_NET_API_KEY",
}
PAID_PLANS = ("standard", "pro", "ultra")
ALL_PLANS = ("free",) + PAID_PLANS
MAX_ADMINS = 2  # total, incluindo o proprietário
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")


class ConfigError(Exception):
    """Erro de configuração. Contém só o nome da variável e o problema."""

    def __init__(self, variable: str, problem: str) -> None:
        super().__init__(f"Configuração inválida: {variable} ({problem})")
        self.variable = variable
        self.problem = problem


def _raw(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def _int(env, name, default, lo, hi) -> int:
    raw = _raw(env, name)
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        raise ConfigError(name, "deve ser um número inteiro") from None
    if not lo <= val <= hi:
        raise ConfigError(name, f"deve estar entre {lo} e {hi}")
    return val


def _float(env, name, default, lo, hi) -> float:
    raw = _raw(env, name)
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        raise ConfigError(name, "deve ser um número") from None
    if not lo <= val <= hi:
        raise ConfigError(name, f"deve estar entre {lo} e {hi}")
    return val


def _bool(env, name, default) -> bool:
    raw = _raw(env, name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "sim", "on"):
        return True
    if raw in ("0", "false", "no", "nao", "não", "off"):
        return False
    raise ConfigError(name, "deve ser verdadeiro/falso (true/false)")


def _id_list(env, name) -> list[int]:
    raw = _raw(env, name)
    out: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            val = int(part)
        except ValueError:
            raise ConfigError(name, "lista de IDs numéricos separados por vírgula") from None
        if val <= 0:
            raise ConfigError(name, "IDs devem ser positivos")
        out.append(val)
    return out


@dataclass(frozen=True)
class Config:
    telegram_token: str = field(repr=False)
    owner_id: int
    admin_ids: tuple[int, ...]
    support_username: str
    support_channel: str
    support_forward_to_admins: bool
    default_provider: str
    default_model: str
    fallback_provider: str
    fallback_model: str
    provider_keys: dict = field(repr=False)
    provider_models: dict
    plan_models: dict
    plan_quotas: dict
    plan_period_days: int
    database_path: str
    log_path: str
    log_level: str
    free_message_limit: int
    group_min_members: int
    group_premium_feature_enabled: bool
    group_premium_plan: str
    groups_enabled_by_default: bool
    api_timeout: float
    api_max_retries: int
    backoff_base: float
    backoff_max: float
    max_output_tokens: int
    history_max_messages: int
    history_max_chars: int
    history_retention_days: int
    max_input_chars: int
    rate_limit_messages: int
    rate_limit_window: float
    default_personality: str

    # ---- carregamento -------------------------------------------------
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, require_token: bool = True) -> "Config":
        env = os.environ if env is None else env

        token = _raw(env, "TELEGRAM_BOT_TOKEN")
        if require_token:
            if not token:
                raise ConfigError("TELEGRAM_BOT_TOKEN", "obrigatória")
            if not TOKEN_RE.match(token):
                raise ConfigError("TELEGRAM_BOT_TOKEN", "formato inválido")

        owner_raw = _raw(env, "OWNER_TELEGRAM_ID")
        if not owner_raw:
            raise ConfigError("OWNER_TELEGRAM_ID", "obrigatória")
        owner = _int(env, "OWNER_TELEGRAM_ID", 0, 1, 2**52)

        admins = [a for a in _id_list(env, "ADMIN_TELEGRAM_IDS") if a != owner]
        admins = list(dict.fromkeys(admins))
        if 1 + len(admins) > MAX_ADMINS:
            raise ConfigError("ADMIN_TELEGRAM_IDS", f"máximo de {MAX_ADMINS} administradores no total (com o proprietário)")

        support = _raw(env, "SUPPORT_USERNAME").lstrip("@")
        if support and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", support):
            raise ConfigError("SUPPORT_USERNAME", "username do Telegram inválido")
        channel = _raw(env, "SUPPORT_CHANNEL")
        if channel and not re.fullmatch(r"(@[A-Za-z0-9_]{5,32}|https://t\.me/[A-Za-z0-9_+/\-]{3,64})", channel):
            raise ConfigError("SUPPORT_CHANNEL", "use @canal ou https://t.me/...")

        provider = (_raw(env, "DEFAULT_PROVIDER") or "openrouter").lower()
        if provider not in PROVIDER_KEY_VARS:
            raise ConfigError("DEFAULT_PROVIDER", "provedor desconhecido")
        fallback = _raw(env, "FALLBACK_PROVIDER").lower()
        if fallback and fallback not in PROVIDER_KEY_VARS:
            raise ConfigError("FALLBACK_PROVIDER", "provedor desconhecido")
        if fallback and fallback == provider:
            raise ConfigError("FALLBACK_PROVIDER", "deve ser diferente de DEFAULT_PROVIDER")

        keys: dict[str, str] = {}
        models: dict[str, str] = {}
        for name, var in PROVIDER_KEY_VARS.items():
            key = _raw(env, var)
            if key:
                if re.search(r"\s", key):
                    raise ConfigError(var, "contém espaços")
                keys[name] = key
            model = _raw(env, f"{name.upper()}_MODEL")
            if model:
                models[name] = model

        quotas = {
            "free": _int(env, "FREE_MESSAGE_LIMIT", 10, 0, 1_000_000),
            "standard": _int(env, "STANDARD_QUOTA", 500, 1, 10_000_000),
            "pro": _int(env, "PRO_QUOTA", 2000, 1, 10_000_000),
            "ultra": _int(env, "ULTRA_QUOTA", 10000, 1, 10_000_000),
        }
        plan_models = {p: _raw(env, f"{p.upper()}_MODEL") for p in PAID_PLANS}
        plan_models["free"] = _raw(env, "FREE_PLAN_MODEL")

        premium_plan = (_raw(env, "GROUP_PREMIUM_PLAN") or "standard").lower()
        if premium_plan not in PAID_PLANS:
            raise ConfigError("GROUP_PREMIUM_PLAN", "use standard, pro ou ultra")

        level = (_raw(env, "LOG_LEVEL") or "INFO").upper()
        if level not in LOG_LEVELS:
            raise ConfigError("LOG_LEVEL", "nível inválido")
        db_path = _raw(env, "DATABASE_PATH") or "data/haxorgpt.db"
        if "\x00" in db_path:
            raise ConfigError("DATABASE_PATH", "caminho inválido")

        return cls(
            telegram_token=token,
            owner_id=owner,
            admin_ids=tuple(admins),
            support_username=support,
            support_channel=channel,
            support_forward_to_admins=_bool(env, "SUPPORT_FORWARD_TO_ADMINS", True),
            default_provider=provider,
            default_model=_raw(env, "DEFAULT_MODEL"),
            fallback_provider=fallback,
            fallback_model=_raw(env, "FALLBACK_MODEL"),
            provider_keys=keys,
            provider_models=models,
            plan_models=plan_models,
            plan_quotas=quotas,
            plan_period_days=_int(env, "PLAN_PERIOD_DAYS", 30, 1, 366),
            database_path=db_path,
            log_path=_raw(env, "LOG_PATH") or "data/logs/haxorgpt.log",
            log_level=level,
            free_message_limit=quotas["free"],
            group_min_members=_int(env, "GROUP_MIN_MEMBERS", 500, 1, 10_000_000),
            group_premium_feature_enabled=_bool(env, "GROUP_PREMIUM_FEATURE_ENABLED", False),
            group_premium_plan=premium_plan,
            groups_enabled_by_default=_bool(env, "GROUPS_ENABLED_BY_DEFAULT", False),
            api_timeout=_float(env, "API_TIMEOUT_SECONDS", 60.0, 1.0, 600.0),
            api_max_retries=_int(env, "API_MAX_RETRIES", 2, 0, 5),
            backoff_base=_float(env, "API_BACKOFF_BASE_SECONDS", 1.0, 0.0, 60.0),
            backoff_max=_float(env, "API_BACKOFF_MAX_SECONDS", 20.0, 0.0, 300.0),
            max_output_tokens=_int(env, "MAX_OUTPUT_TOKENS", 1024, 16, 32768),
            history_max_messages=_int(env, "HISTORY_MAX_MESSAGES", 20, 0, 200),
            history_max_chars=_int(env, "HISTORY_MAX_CHARS", 12000, 500, 200000),
            history_retention_days=_int(env, "HISTORY_RETENTION_DAYS", 30, 1, 3650),
            max_input_chars=_int(env, "MAX_INPUT_CHARS", 4000, 10, 20000),
            rate_limit_messages=_int(env, "RATE_LIMIT_MESSAGES", 5, 1, 1000),
            rate_limit_window=_float(env, "RATE_LIMIT_WINDOW_SECONDS", 30.0, 1.0, 3600.0),
            default_personality=_raw(env, "DEFAULT_PERSONALITY"),
        )

    # ---- consultas ----------------------------------------------------
    def configured_providers(self) -> list[str]:
        return [p for p in PROVIDER_KEY_VARS if p in self.provider_keys]

    def api_key(self, provider: str) -> str:
        return self.provider_keys.get(provider, "")

    def secret_values(self) -> list[str]:
        vals = list(self.provider_keys.values())
        if self.telegram_token:
            vals.append(self.telegram_token)
        return vals

    def model_for(self, provider: str, plan_model: str = "", fallback: bool = False) -> str:
        """Resolve o modelo: modelo do plano (só provedor principal) >
        modelo do provedor (<NOME>_MODEL) > FALLBACK_MODEL/DEFAULT_MODEL.
        Retorna '' se nada estiver configurado (nenhum nome é inventado)."""
        if plan_model and not fallback:
            return plan_model
        if provider in self.provider_models:
            return self.provider_models[provider]
        return (self.fallback_model if fallback else self.default_model) or ""
