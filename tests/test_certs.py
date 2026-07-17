"""Tests for lib.certs shared certificate-expiry helpers."""

from datetime import UTC, datetime

from lib.certs import cert_domain, expiring_certs, parse_expiry

NOW = datetime(2026, 7, 16, tzinfo=UTC)


def _cert(expires_on, **extra):
    return {"expires_on": expires_on, **extra}


def test_parse_expiry_handles_z_and_fractional_and_naive():
    assert (dt := parse_expiry("2026-08-01T00:00:00Z")) is not None
    assert dt.tzinfo is not None
    assert (dt := parse_expiry("2026-08-01T00:00:00.000+00:00")) is not None
    assert dt.tzinfo is not None
    # Naive (NPM MySQL shape) gets UTC attached rather than raising later.
    assert (dt := parse_expiry("2026-08-01 00:00:00")) is not None
    assert dt.tzinfo == UTC
    assert parse_expiry("") is None
    assert parse_expiry("not-a-date") is None


def test_cert_domain_fallbacks():
    assert cert_domain({"nice_name": "web"}) == "web"
    assert cert_domain({"domain_names": ["a.example"]}) == "a.example"
    assert cert_domain({}) == "?"


def test_expiring_certs_filters_and_scores():
    certs = [
        _cert("2026-07-20T00:00:00Z", nice_name="soon"),  # 4 days -> critical
        _cert("2026-08-05T00:00:00Z", nice_name="warn"),  # 20 days -> warning
        _cert("2027-01-01T00:00:00Z", nice_name="later"),  # outside window
        _cert("", nice_name="none"),  # unparseable
    ]
    result = expiring_certs(certs, now=NOW)

    assert [r["domain"] for r in result] == ["soon", "warn"]
    assert result[0]["severity"] == "critical"
    assert result[0]["days_left"] == 4
    assert result[1]["severity"] == "warning"


def test_expiring_certs_non_list_returns_empty():
    assert expiring_certs({"error": "boom"}) == []
