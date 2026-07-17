"""Shared parsing for the Healthchecks API.

``/api/v3/checks/`` returns a bare list on some deployments and
``{"checks": [...]}`` on others; four call sites (healthchecks, changefeed,
health, lib/refresh) each re-did that unwrap, and two derived a check's UUID
from its ping_url with the same unique_key/slug fallback. This owns both.
"""


def unwrap_checks(data) -> list:
    """Normalize a /api/v3/checks/ response to a list of check dicts."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("checks", [])
    return []


def check_uuid(check: dict) -> str | None:
    """Derive a check's UUID from its ping_url, falling back to unique_key/slug."""
    ping_url = check.get("ping_url") or ""
    uuid = ping_url.rsplit("/", 1)[-1] if ping_url else None
    return uuid or check.get("unique_key") or check.get("slug")


def unwrap_flips(data) -> list:
    """Normalize a /api/v3/checks/{uuid}/flips/ response to a list of flips."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("flips", [])
    return []
