"""Freshness metadata helper for tool responses (D-15, D-16)."""

import calendar
import time

import config


def staleness(source: str) -> dict | None:
    """Compute refresh staleness for a source, or None if it has no timestamp.

    Returns ``{last_refreshed, age_seconds, stale}``. Used by both
    :func:`build_meta` and ``show_data_freshness`` so the
    strptime/age/threshold math lives in one place. ``source`` names a
    ``config.REFRESH_TIMESTAMPS`` key; ``knowledge`` maps to ``registries`` and
    ``docs``/``stacks``/``vault`` map to ``docs`` for the timestamp lookup.
    """
    last_refreshed = config.REFRESH_TIMESTAMPS.get(source)
    if last_refreshed is None:
        if source in ("knowledge",):
            last_refreshed = config.REFRESH_TIMESTAMPS.get("registries")
        elif source in ("docs", "stacks", "vault"):
            last_refreshed = config.REFRESH_TIMESTAMPS.get("docs")
    if not last_refreshed:
        return None

    refreshed_ts = calendar.timegm(time.strptime(last_refreshed, "%Y-%m-%dT%H:%M:%SZ"))
    age_seconds = time.time() - refreshed_ts
    threshold = config.STALENESS_THRESHOLDS.get(source)
    if threshold is None:
        if source in ("docs", "stacks", "vault"):
            threshold = config.STALENESS_THRESHOLDS.get("default_docs", 7200)
        else:
            threshold = config.STALENESS_THRESHOLDS.get("default_registries", 1800)
    return {
        "last_refreshed": last_refreshed,
        "age_seconds": age_seconds,
        "stale": age_seconds > threshold,
    }


def build_meta(
    source: str,
    data_window: str | None = None,
    confidence: str = "high",
) -> dict:
    """Build _meta dict for tool responses.

    Args:
        source: Data source name (e.g., "prometheus", "plex", "healthchecks")
        data_window: Time window of the data (e.g., "5m", "24h", "7d"). None for point-in-time.
        confidence: Data confidence level - "high" (fresh, complete), "medium" (partial or slightly stale), "low" (error or very stale)

    Returns:
        Dict with source, queried_at (ISO UTC), data_window, confidence.
        When refresh timestamps exist for the source, also includes last_refreshed and stale.
    """
    meta = {
        "source": source,
        "queried_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "data_window": data_window,
        "confidence": confidence,
    }

    # Add staleness for sources with refresh tracking
    st = staleness(source)
    if st:
        meta["last_refreshed"] = st["last_refreshed"]
        meta["stale"] = st["stale"]

    return meta
