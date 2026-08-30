from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys

from dotenv import load_dotenv
from telegram import Update

from .config import Settings
from .database import Database
from .service import EnglishTeacherService
from .telegram_app import build_application


LOG = logging.getLogger("english_teacher")
TELEGRAM_TOKEN_RE = re.compile(r"(?i)(bot)\d+:[A-Za-z0-9_-]+")


class RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = TELEGRAM_TOKEN_RE.sub(r"\1<redacted>", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


async def initialize_only(settings: Settings) -> None:
    database = Database(settings)
    await database.initialize()
    connection = await database.connect()
    try:
        await connection.execute("SELECT 1")
    finally:
        await connection.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Private Telegram English coach with local RAG memory and Anki export."
    )
    parser.add_argument("--check", action="store_true", help="Valide toute la configuration et initialise SQLite")
    parser.add_argument("--healthcheck", action="store_true", help="Vérifie uniquement l'accès à SQLite")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        settings.validate(require_secrets=not args.healthcheck)
    except ValueError as exc:
        print(f"Configuration invalide: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactSecretsFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    asyncio.run(initialize_only(settings))
    if args.check or args.healthcheck:
        print(f"OK: configuration valide, SQLite accessible ({settings.database_path})")
        return

    database = Database(settings)
    service = EnglishTeacherService(settings, database)
    application = build_application(settings, service)
    LOG.info(
        "Démarrage bot: TZ=%s, matin=%s %02d:%02d, soir=%s %02d:%02d, Anki=%s",
        settings.timezone,
        settings.morning_enabled,
        *settings.morning_clock,
        settings.evening_enabled,
        *settings.evening_clock,
        settings.anki_enabled,
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
