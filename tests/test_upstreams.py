"""Tests for shared upstream-parsing helpers (lib.crowdsec, lib.healthchecks)."""

from lib.crowdsec import is_community, local_bans
from lib.healthchecks import check_uuid, unwrap_checks, unwrap_flips


def test_crowdsec_local_bans_filters_capi_and_non_bans():
    decisions = [
        {"origin": "crowdsec", "type": "ban", "value": "1.2.3.4"},
        {"origin": "CAPI", "type": "ban", "value": "5.6.7.8"},
        {"origin": "crowdsec", "type": "captcha", "value": "9.9.9.9"},
    ]
    bans = local_bans(decisions)
    assert len(bans) == 1
    assert bans[0]["value"] == "1.2.3.4"
    assert is_community({"origin": "CAPI"}) is True
    assert local_bans(None) == []


def test_unwrap_checks_handles_list_and_dict():
    assert unwrap_checks([{"name": "a"}]) == [{"name": "a"}]
    assert unwrap_checks({"checks": [{"name": "b"}]}) == [{"name": "b"}]
    assert unwrap_checks("nonsense") == []


def test_check_uuid_from_ping_url_and_fallbacks():
    assert check_uuid({"ping_url": "https://hc/abcd-uuid"}) == "abcd-uuid"
    assert check_uuid({"unique_key": "uk"}) == "uk"
    assert check_uuid({"slug": "sl"}) == "sl"
    assert check_uuid({}) is None


def test_unwrap_flips_handles_list_and_dict():
    assert unwrap_flips([{"up": 1}]) == [{"up": 1}]
    assert unwrap_flips({"flips": [{"up": 0}]}) == [{"up": 0}]
    assert unwrap_flips(None) == []
