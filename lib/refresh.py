"""Refresh engine facade: composes registry and content refresh.

The implementation lives in lib/refresh_registries.py (live-API service/host
registry refresh) and lib/refresh_content.py (Gitea docs/stacks/vault sync).
This module re-exports their entrypoints and owns periodic_refresh, the
background task that drives both.
"""

import asyncio
import logging
import time

import config
from lib.refresh_content import refresh_docs_impl
from lib.refresh_registries import refresh_registries_impl

__all__ = ["refresh_registries_impl", "refresh_docs_impl", "periodic_refresh"]

_logger = logging.getLogger(__name__)


async def periodic_refresh(clients: dict) -> None:
    """Background task: periodically refresh registries and docs."""
    interval = config.REFRESH_INTERVAL_SECONDS
    doc_interval = config.DOC_REFRESH_INTERVAL_SECONDS
    last_doc_refresh = 0.0

    # Initial refresh on startup
    try:
        await refresh_registries_impl(clients)
        _logger.info("Initial registry refresh complete")
    except Exception:
        _logger.exception("Initial registry refresh failed")

    try:
        await refresh_docs_impl(clients)
        last_doc_refresh = time.time()
        _logger.info("Initial doc refresh complete")
    except Exception:
        _logger.exception("Initial doc refresh failed")

    # Periodic loop
    while True:
        await asyncio.sleep(interval)
        try:
            diff = await refresh_registries_impl(clients)
            _logger.info("Registry refresh: %s", diff)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("Background registry refresh failed")

        if time.time() - last_doc_refresh >= doc_interval:
            try:
                diff = await refresh_docs_impl(clients)
                last_doc_refresh = time.time()
                _logger.info("Doc refresh: %s", diff)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Background doc refresh failed")
