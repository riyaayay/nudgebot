"""Entrypoint for NudgeBot.
Wires config -> db -> telegram bot -> scheduler -> health server.
Handles SIGTERM/SIGINT gracefully and shuts down components in the proper order.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from config import ConfigError, configure_logging, load_settings
from crypto_utils import CredentialCipher
from db import Database
from health import HealthServer
from scheduler import build_scheduler
from telegram_bot import BotContext, build_application

logger = logging.getLogger(__name__)


async def run() -> None:
    # 1. Load settings and configure logging
    try:
        settings = load_settings()
    except ConfigError as exc:
        # Standard logging not yet configured; write directly to stderr
        sys.stderr.write(f"Configuration error: {exc}\n")
        sys.exit(1)

    configure_logging(settings.log_level, settings.log_format)
    settings.print_redacted_summary()

    # 2. Initialize encryption cipher
    try:
        cipher = CredentialCipher(settings.encryption_keys)
    except ValueError as exc:
        logger.critical("Fatal encryption key error: %s", exc)
        sys.exit(1)

    # 3. Initialize database
    try:
        db = Database(settings.database_path)
    except Exception as exc:
        logger.critical("Fatal database initialization error: %s", exc)
        sys.exit(1)

    # 4. Build Telegram application
    bc = BotContext(db=db, cipher=cipher, settings=settings)
    app = build_application(settings.telegram_bot_token, bc)

    # 5. Build APScheduler
    scheduler = build_scheduler(
        db=db,
        cipher=cipher,
        bot=app.bot,
        settings=settings,
    )

    # 6. Start health check server
    health_server = HealthServer(
        port=settings.health_port,
        scheduler_interval_seconds=60,
    )

    # Shutdown coordination
    stop_event = asyncio.Event()

    def _trigger_shutdown(sig_name: str) -> None:
        logger.info("Received %s signal. Initiating graceful shutdown...", sig_name)
        stop_event.set()

    # Register OS signals
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _trigger_shutdown, sig.name)
        except NotImplementedError:
            # Fallback for Windows
            signal.signal(sig, lambda s, f: _trigger_shutdown(sig.name))

    # 7. Start application components
    await health_server.start()
    scheduler.start()
    logger.info("Scheduler started (1-min tick, 15-min reminder sweep).")

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("NudgeBot is online and polling Telegram for updates.")

        # Wait until termination signal is received
        await stop_event.wait()

        # Graceful shutdown sequence
        logger.info("Stopping Telegram updater...")
        await app.updater.stop()

        logger.info("Stopping Telegram application...")
        await app.stop()

        logger.info("Stopping health server...")
        await health_server.stop()

        logger.info("Shutting down scheduler...")
        scheduler.shutdown(wait=True)

        logger.info("Closing database...")
        db.close()

    logger.info("NudgeBot has shut down cleanly. Bye!")


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
