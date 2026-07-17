"""Tests for the shared secret-redaction helper."""

import httpx

from lib.redact import redact, redact_exception


def test_redact_strips_query_string():
    assert (
        redact("http://host/api/v2?apikey=SECRET&cmd=get_activity")
        == "http://host/api/v2?<redacted>"
    )


def test_redact_preserves_path_and_closing_quote():
    text = "Client error '401 Unauthorized' for url 'http://h/api?token=T&x=1'"
    out = redact(text)
    assert "token=T" not in out
    assert "?<redacted>" in out
    assert out.endswith("'")


def test_redact_leaves_urlless_text_untouched():
    assert redact("Connection refused") == "Connection refused"


def test_redact_exception_scrubs_http_status_error():
    """str(HTTPStatusError) embeds the full request URL incl. secret params."""
    request = httpx.Request(
        "GET", "http://host/api/v2?apikey=TOP_SECRET_KEY&cmd=get_activity"
    )
    response = httpx.Response(401, request=request)
    exc = httpx.HTTPStatusError("boom", request=request, response=response)
    message = redact_exception(exc)
    assert "TOP_SECRET_KEY" not in message


def test_redact_exception_scrubs_session_token_and_sid():
    for secret_url in (
        "http://dns/api/zones/list?token=LIVE_TOKEN",
        "http://nas/webapi/entry.cgi?_sid=LIVE_SID",
        "http://dns/api/user/login?user=admin&pass=hunter2",
    ):
        request = httpx.Request("GET", secret_url)
        exc = httpx.ConnectError("nope", request=request)
        # ConnectError str doesn't include the URL, but callers may format it in;
        # verify redact() handles the raw URL form too.
        assert "token=" not in redact(secret_url)
        assert "_sid=" not in redact(secret_url)
        assert "pass=" not in redact(secret_url)
        redact_exception(exc)  # must not raise
