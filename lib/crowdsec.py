"""Shared CrowdSec decision parsing.

The "keep local bans, drop the CAPI community blocklist" filter was written
three times (aggregation, health, changefeed) with slight divergences. This
owns the predicate once.
"""


def is_community(decision: dict) -> bool:
    """True if a decision comes from the CAPI community blocklist (not a local ban)."""
    return decision.get("origin") == "CAPI"


def local_bans(decisions) -> list[dict]:
    """Local (non-CAPI) ban decisions from a /v1/decisions payload."""
    return [
        d for d in (decisions or []) if not is_community(d) and d.get("type") == "ban"
    ]
