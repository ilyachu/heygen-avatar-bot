from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient

from workers.metrics_collector import MetricsCollector, TelethonMetricsClient

logger = logging.getLogger(__name__)


async def collect_metrics_forever(
    db_path: str,
    *,
    session_path: str,
    api_id: int,
    api_hash: str,
    interval_seconds: int = 900,
) -> None:
    if not api_id or not api_hash or not session_path:
        logger.warning("Post metrics disabled: Telethon credentials are not configured")
        return
    client = TelegramClient(session_path, api_id, api_hash)
    while True:
        try:
            await client.connect()
            break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Post metrics connection failed; retrying")
            await asyncio.sleep(interval_seconds)
    try:
        if not await client.is_user_authorized():
            logger.warning("Post metrics disabled: Telethon session is not authorized")
            return
        collector = MetricsCollector(
            db_path, TelethonMetricsClient(client), provider="telethon"
        )
        while True:
            try:
                results = await collector.run_due_windows()
                collected = sum(item.collected for item in results.values())
                failed = sum(item.failed for item in results.values())
                if collected or failed:
                    logger.info(
                        "Post metrics collection finished: collected=%s failed=%s",
                        collected,
                        failed,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Post metrics collection iteration failed")
            await asyncio.sleep(interval_seconds)
    finally:
        await client.disconnect()
