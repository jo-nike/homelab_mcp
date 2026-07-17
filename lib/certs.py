"""Shared SSL-certificate expiry parsing for NPM certificate payloads.

NPM's ``/api/nginx/certificates`` entries carry ``expires_on`` as an ISO-8601
string, sometimes with a trailing ``.000+00:00`` or a ``Z`` zone. Four modules
(npm, health, changefeed, aggregation) each re-implemented the
parse/threshold/days-left/severity math and had already drifted (different tz
handling, domain fallbacks, and a 7-day critical tier only some carried). This
owns that logic once; call sites format the returned data into their own shape.
"""

from datetime import UTC, datetime, timedelta


def parse_expiry(expires_on: str | None) -> datetime | None:
    """Parse an NPM ``expires_on`` string to a tz-aware datetime, or None.

    Handles the ``Z`` suffix and NPM's ``.000+00:00`` fractional form, and
    attaches UTC when the value parses as a naive datetime (NPM's MySQL
    ``YYYY-MM-DD HH:MM:SS`` shape) so later comparisons never raise.
    """
    if not expires_on:
        return None
    text = expires_on.replace("Z", "+00:00").replace(".000+00:00", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def cert_domain(cert: dict) -> str:
    """Best display name for a certificate: nice_name, else first domain, else ?."""
    names = cert.get("domain_names") or []
    return cert.get("nice_name") or (names[0] if names else "?")


def expiring_certs(
    certs,
    warn_days: int = 30,
    crit_days: int = 7,
    now: datetime | None = None,
) -> list[dict]:
    """Certs expiring within ``warn_days``, as {domain, expires_on, days_left, severity}.

    ``severity`` is ``"critical"`` when ``days_left < crit_days`` else
    ``"warning"``. Non-list input (e.g. an error dict) yields an empty list.
    """
    if not isinstance(certs, list):
        return []
    now = now or datetime.now(UTC)
    threshold = now + timedelta(days=warn_days)
    out: list[dict] = []
    for c in certs:
        expires_on = c.get("expires_on", "")
        exp_dt = parse_expiry(expires_on)
        if exp_dt is None or exp_dt >= threshold:
            continue
        days_left = max(0, (exp_dt - now).days)
        out.append(
            {
                "domain": cert_domain(c),
                "expires_on": expires_on,
                "days_left": days_left,
                "severity": "critical" if days_left < crit_days else "warning",
            }
        )
    return out
