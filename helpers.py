"""Utilitários de teste: ambiente mínimo com banco temporário e mocks."""
import asyncio
from types import SimpleNamespace

from admin import AdminService
from ai import AIService
from api_providers import ChatClient, ProviderRouter
from config import Config
from database import Database
from groups import GroupService
from security import RateLimiter
from subscriptions import SubscriptionService

TOKEN = "123456789:" + "A" * 35
OWNER = 1001


def make_env(extra=None):
    env = {"TELEGRAM_BOT_TOKEN": TOKEN, "OWNER_TELEGRAM_ID": str(OWNER), "DEFAULT_PROVIDER": "groq",
           "DEFAULT_MODEL": "modelo-teste", "GROQ_API_KEY": "gsk_testkey1234567890abcd",
           "API_BACKOFF_BASE_SECONDS": "0", "API_MAX_RETRIES": "2"}
    env.update(extra or {})
    return env


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeResp:
    def __init__(self, status=200, content="ok", headers=None, model="m-reportado"):
        self.status_code, self.headers = status, headers or {}
        self._data = {"choices": [{"message": {"content": content}}], "model": model}

    def json(self):
        return self._data


class FakeHTTP:
    """Cliente HTTP falso: executa uma fila de respostas/exceções por URL."""
    def __init__(self, script):
        self.script, self.calls = list(script), []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        item = self.script.pop(0) if self.script else FakeResp()
        if isinstance(item, BaseException):
            raise item
        return item


class ReadTimeout(Exception):  # nome imita httpx.ReadTimeout
    pass


def make_services(tmp_path, extra_env=None, script=None, clock=None):
    config = Config.from_env(make_env(extra_env))
    clock = clock or FakeClock()
    db = Database(str(tmp_path / "t.db"), clock=clock)
    db.init()
    db.sync_plans(config)
    subs = SubscriptionService(db, clock=clock)
    admin = AdminService(db, config, subs)
    admin.bootstrap()
    http = FakeHTTP(script or [])
    client = ChatClient(config, http_client=http, sleep=_nosleep, rng=lambda: 0.5)
    groups = GroupService(db, config, clock=clock)
    ai = AIService(config, db, subs, ProviderRouter(config, client), groups, is_admin=admin.is_admin,
                   limiter=RateLimiter(1000, 1))
    return SimpleNamespace(config=config, db=db, subs=subs, admin=admin, groups=groups, ai=ai,
                           client=client, http=http, clock=clock)


async def _nosleep(_):
    return None


def run(coro):
    return asyncio.run(coro)
