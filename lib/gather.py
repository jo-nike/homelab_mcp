"""Shared parallel-gather helper for the cross-service aggregate tools.

``aggregation``, ``health``, and ``changefeed`` each defined their own
``_safe_gather`` with silently divergent semantics: aggregation mapped a failed
coroutine to an error dict, the other two mapped it to an empty list. This owns
one implementation with the failure representation selectable via ``on_error``.
"""

import asyncio
from collections.abc import Callable
from typing import Any

from lib.redact import redact_exception


def error_dict(exc: Exception) -> dict:
    """``on_error`` mapper: represent a failed source as an error dict."""
    return {"error": "unreachable", "message": redact_exception(exc)}


async def safe_gather(
    *coros,
    on_error: Callable[[Exception], Any] = lambda exc: [],
) -> list:
    """Run coroutines concurrently, replacing any raised exception with
    ``on_error(exc)`` so one failing source never aborts the others.

    The default ``on_error`` yields an empty list (the health/changefeed
    convention). Pass ``on_error=error_dict`` for the aggregation convention.
    """
    results = await asyncio.gather(*coros, return_exceptions=True)
    return [on_error(r) if isinstance(r, Exception) else r for r in results]
