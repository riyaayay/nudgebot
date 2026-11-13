"""Minimal zero-dependency HTTP health check server using asyncio.start_server.
Responds on /healthz and / for Docker HEALTHCHECK and monitoring tools.
Fails with 503 if the scheduler has not ticked within 3x its interval.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from scheduler import get_last_scheduler_tick

logger = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, port: int, scheduler_interval_seconds: int = 60):
        self.port = port
        self.interval_seconds = scheduler_interval_seconds
        self._server: asyncio.Server | None = None
        self._start_time = datetime.now(UTC)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, "0.0.0.0", self.port)
        logger.info("Health server listening on http://0.0.0.0:%d/healthz", self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Health server stopped.")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # Read incoming HTTP request line & headers (GET /healthz HTTP/1.1)
            await asyncio.wait_for(reader.read(2048), timeout=4.0)

            now = datetime.now(UTC)
            last_tick = get_last_scheduler_tick()

            # Max allowed elapsed time is 3 * tick interval (default 3 * 60s = 180s)
            max_stale_seconds = self.interval_seconds * 3

            status_code = 200
            status_text = "OK"
            body_dict = {
                "status": "healthy",
                "uptime_seconds": round((now - self._start_time).total_seconds()),
                "last_scheduler_tick": last_tick.isoformat() if last_tick else None,
            }

            if last_tick is None:
                # Still within startup grace period?
                uptime = (now - self._start_time).total_seconds()
                if uptime > max_stale_seconds:
                    status_code = 503
                    status_text = "Service Unavailable"
                    body_dict["status"] = "unhealthy"
                    body_dict["reason"] = "Scheduler has never completed a tick"
            else:
                elapsed = (now - last_tick).total_seconds()
                if elapsed > max_stale_seconds:
                    status_code = 503
                    status_text = "Service Unavailable"
                    body_dict["status"] = "unhealthy"
                    body_dict["reason"] = f"Scheduler tick stalled ({round(elapsed)}s ago)"

            body_bytes = json.dumps(body_dict).encode("utf-8")
            response = (
                f"HTTP/1.1 {status_code} {status_text}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body_bytes)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode() + body_bytes

            writer.write(response)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
