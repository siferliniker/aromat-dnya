"""Run with python -m miniapp; the existing encrypted catalogue must be initialized."""

import logging
import os
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from sqlalchemy import select

from config import load_config
from miniapp.app import create_app
from miniapp.performance import FragellaPerformance
from storage.models import Perfume
from storage.runtime import create_encrypted_engine


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.CRITICAL)
    engine = None
    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        owner = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not owner.isdigit() or int(owner) <= 0:
            raise ValueError("owner credentials unavailable")
        engine = create_encrypted_engine(os.environ.get("DATABASE_URL", "sqlite:///data/private.sqlite3"),
                                         os.environ.get("DATABASE_ENCRYPTION_KEY"))
        with engine.connect() as connection:
            if connection.scalar(select(Perfume.id).limit(1)) is None:
                raise ValueError("catalogue unavailable")
        config = load_config(os.environ.get("CONFIG_PATH", "config.yaml"))
        app = create_app(engine=engine, bot_token=token, owner_id=int(owner), config=config,
                         performance=FragellaPerformance(os.environ.get("FRAGELLA_API_KEY", ""), engine))
        web.run_app(app, host="0.0.0.0", port=8000, access_log=None,
                    print=lambda _message: print("Ароматия Mini App готова на порту 8000."))
        return 0
    except Exception:
        print("Mini App не запущена: проверьте локальные настройки и готовность зашифрованного каталога.")
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
