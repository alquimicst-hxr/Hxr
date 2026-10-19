"""Ponto de entrada: configuração, logs seguros, banco, aplicação e polling."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

EXIT_OK, EXIT_ERROR, EXIT_CONFIG = 0, 1, 2


def setup_logging(config) -> None:
    from security import RedactingFormatter
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", secrets=config.secret_values())
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_path = Path(config.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers.append(RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"))
    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(config.log_level)
    # httpx/httpcore registram URLs (a do Telegram contém o token): silenciar
    for noisy in ("httpx", "httpcore", "telegram.ext.Application"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    from config import Config, ConfigError
    try:
        config = Config.from_env()
    except ConfigError as err:
        print(f"ERRO: {err}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        setup_logging(config)
        log = logging.getLogger("main")
        from admin import AdminService
        from ai import AIService
        from api_providers import ChatClient, ProviderRouter
        from bot import LunaBot
        from database import Database
        from groups import GroupService
        from subscriptions import SubscriptionService

        db = Database(config.database_path)
        db.init()
        db.sync_plans(config)
        subs = SubscriptionService(db)
        admin = AdminService(db, config, subs)
        admin.bootstrap()
        groups = GroupService(db, config)
        client = ChatClient(config)
        ai = AIService(config, db, subs, ProviderRouter(config, client), groups, is_admin=admin.is_admin)
        if not config.configured_providers():
            log.warning("nenhuma chave de provedor de IA configurada: gerações falharão até configurar")
        app = LunaBot(config, db, subs, admin, groups, ai, client).build()
        log.info("iniciando polling")
        app.run_polling(allowed_updates=["message", "callback_query", "my_chat_member"])
        return EXIT_OK
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception as err:  # noqa: BLE001
        from security import redact_secrets
        logging.getLogger("main").error("falha na inicialização: %s: %s", type(err).__name__,
                                        redact_secrets(str(err), config.secret_values()))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
