"""Secret redaction for error messages surfaced to clients and logs.

httpx exception strings embed the full request URL, including query strings
that carry session tokens (``?token=``, ``?_sid=``), API keys (``?apikey=``),
and login credentials (``?pass=``/``?passwd=``). Returning ``str(exc)`` verbatim
in a tool result (which reaches LLM transcripts, including cloud LLMs) or
logging it (container logs flow into Loki and back out through this server's own
log tools) leaks those secrets.

``redact_exception`` strips the query string from any URL-like substring so the
remaining text is safe to surface. It is deliberately aggressive: the whole
query is removed rather than individual known-secret params, so a future
secret-carrying param cannot slip through.
"""

import re

# Matches a URL query string: a '?' followed by any non-whitespace,
# non-quote, non-angle-bracket run (so the closing quote/space that follows a
# URL inside an exception message is preserved).
_QUERY_RE = re.compile(r"\?[^\s\"'<>]*")


def redact(text: str) -> str:
    """Return ``text`` with URL query strings replaced by ``?<redacted>``."""
    return _QUERY_RE.sub("?<redacted>", str(text))


def redact_exception(exc: BaseException) -> str:
    """Return ``str(exc)`` with any URL query strings removed.

    Use in place of ``str(e)`` anywhere an exception message is put into a tool
    result or a log line for a client whose request URLs carry secrets.
    """
    return redact(str(exc))
