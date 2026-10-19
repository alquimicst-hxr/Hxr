"""Adaptadores dos provedores de IA.

Todos os provedores listados expõem (segundo suas documentações públicas)
um endpoint de chat completions compatível com o formato OpenAI. Em vez de
depender do SDK, usamos httpx direto, com erros classificados, retry
limitado (backoff exponencial com jitter) e fallback seletivo.

ATENÇÃO: as URLs base abaixo NÃO puderam ser validadas online durante o
desenvolvimento (sem rede). Confirme cada uma na documentação oficial do
provedor antes de produção (veja README, seção "Validação pendente").
Nenhum nome de modelo é embutido: configure DEFAULT_MODEL / <PROVEDOR>_MODEL.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    display: str
    base_url: str  # sem a barra final; o caminho /chat/completions é adicionado


PROVIDERS: dict[str, ProviderSpec] = {s.name: s for s in [
    ProviderSpec("openrouter", "OpenRouter", "https://openrouter.ai/api/v1"),
    ProviderSpec("groq", "Groq", "https://api.groq.com/openai/v1"),
    ProviderSpec("together", "Together AI", "https://api.together.xyz/v1"),
    ProviderSpec("deepseek", "DeepSeek", "https://api.deepseek.com/v1"),
    ProviderSpec("mistral", "Mistral AI", "https://api.mistral.ai/v1"),
    ProviderSpec("fireworks", "Fireworks AI", "https://api.fireworks.ai/inference/v1"),
    ProviderSpec("cerebras", "Cerebras", "https://api.cerebras.ai/v1"),
    ProviderSpec("xai", "xAI", "https://api.x.ai/v1"),
    ProviderSpec("nvidia", "NVIDIA", "https://integrate.api.nvidia.com/v1"),
    ProviderSpec("huggingface", "Hugging Face", "https://router.huggingface.co/v1"),
    ProviderSpec("inference_net", "Inference.net", "https://api.inference.net/v1"),
]}

RETRYABLE = {"rate_limit", "unavailable", "timeout"}
FALLBACK_KINDS = {"rate_limit", "unavailable", "timeout", "auth", "config"}
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@\-]{0,199}$")


class ProviderError(Exception):
    """kind: auth | unavailable | timeout | rate_limit | config | bad_request | bad_response"""

    def __init__(self, kind: str, provider: str, status: Optional[int] = None, retry_after: Optional[float] = None) -> None:
        super().__init__(f"{provider}: {kind}" + (f" (HTTP {status})" if status else ""))
        self.kind, self.provider, self.status, self.retry_after = kind, provider, status, retry_after


@dataclass(frozen=True)
class Completion:
    text: str
    provider: str
    requested_model: str
    reported_model: Optional[str]  # somente o que o provedor informou na resposta


def _classify_exception(exc: BaseException) -> str:
    names = {c.__name__ for c in type(exc).__mro__}
    if names & {"TimeoutException", "ReadTimeout", "ConnectTimeout", "PoolTimeout", "WriteTimeout", "TimeoutError"}:
        return "timeout"
    return "unavailable"


def _status_kind(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "config"  # endpoint ou modelo inexistente
    if status == 408:
        return "timeout"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "unavailable"
    return "bad_request"


def _retry_after(headers: Any) -> Optional[float]:
    try:
        raw = headers.get("retry-after") if headers else None
        return float(raw) if raw is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


class ChatClient:
    def __init__(self, config: Config, http_client: Any = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 rng: Callable[[], float] = random.random) -> None:
        self.config, self._http, self._sleep, self._rng = config, http_client, sleep, rng

    def _client(self):
        if self._http is None:
            import httpx  # import tardio: testes não exigem httpx
            self._http = httpx.AsyncClient(timeout=self.config.api_timeout)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and hasattr(self._http, "aclose"):
            await self._http.aclose()

    def validate(self, provider: str, model: str) -> ProviderSpec:
        spec = PROVIDERS.get(provider)
        if spec is None or not spec.base_url.startswith("https://"):
            raise ProviderError("config", provider)
        if not self.config.api_key(provider):
            raise ProviderError("config", provider)
        if not model or not _MODEL_RE.match(model):
            raise ProviderError("config", provider)
        return spec

    def _delay(self, attempt: int, retry_after: Optional[float]) -> Optional[float]:
        cap = self.config.backoff_max
        base = min(cap, self.config.backoff_base * (2 ** attempt))
        wait = base / 2 + self._rng() * base / 2  # jitter
        if retry_after is not None:
            if retry_after > cap:
                return None  # espera pedida é longa demais: não insistir
            wait = max(wait, retry_after)
        return wait

    async def complete(self, provider: str, model: str, messages: list[dict],
                       max_tokens: Optional[int] = None) -> Completion:
        spec = self.validate(provider, model)
        url = f"{spec.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.config.api_key(provider)}", "Content-Type": "application/json"}
        body = {"model": model, "messages": messages, "max_tokens": max_tokens or self.config.max_output_tokens}
        attempt = 0
        while True:
            try:
                return await self._once(spec, url, headers, body, model)
            except ProviderError as err:
                if err.kind not in RETRYABLE or attempt >= self.config.api_max_retries:
                    raise
                wait = self._delay(attempt, err.retry_after)
                if wait is None:
                    raise
                log.warning("provedor=%s erro=%s tentativa=%d; nova tentativa em %.1fs", provider, err.kind, attempt + 1, wait)
                await self._sleep(wait)
                attempt += 1

    async def _once(self, spec: ProviderSpec, url: str, headers: dict, body: dict, model: str) -> Completion:
        try:
            resp = await self._client().post(url, json=body, headers=headers, timeout=self.config.api_timeout)
        except Exception as exc:  # noqa: BLE001 - classificado por nome, sem vazar detalhes
            raise ProviderError(_classify_exception(exc), spec.name) from None
        status = resp.status_code
        if status >= 400:
            raise ProviderError(_status_kind(status), spec.name, status, _retry_after(getattr(resp, "headers", None)))
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001
            raise ProviderError("bad_response", spec.name, status) from None
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("bad_response", spec.name, status)
        reported = data.get("model") if isinstance(data.get("model"), str) else None
        return Completion(text.strip(), spec.name, model, reported)


class ProviderRouter:
    """Provedor principal + alternativo opcional (fallback só para falhas apropriadas)."""

    def __init__(self, config: Config, client: ChatClient) -> None:
        self.config, self.client = config, client

    async def generate(self, messages: list[dict], max_tokens: Optional[int] = None, plan_model: str = "") -> Completion:
        cfg = self.config
        attempts = [(cfg.default_provider, cfg.model_for(cfg.default_provider, plan_model))]
        if cfg.fallback_provider:
            attempts.append((cfg.fallback_provider, cfg.model_for(cfg.fallback_provider, fallback=True)))
        last: Optional[ProviderError] = None
        for provider, model in attempts:
            try:
                return await self.client.complete(provider, model, messages, max_tokens)
            except ProviderError as err:
                last = err
                log.error("falha no provedor %s: %s", provider, err.kind)
                if err.kind not in FALLBACK_KINDS:
                    break
        assert last is not None
        raise last
