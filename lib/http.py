"""Shared HTTP request helper for tool modules.

Replaces the ~20 near-identical per-module ``_get`` closures (fetch the client
from typing import Any
from ``ctx.lifespan_context``, issue the request, ``raise_for_status``, return
``json()``, map ``TimeoutException``/``HTTPStatusError``/``HTTPError`` to the
standard error dicts) with one ``service_request`` implementing that ladder
once.

Exception messages are routed through :mod:`lib.redact` so query-string secrets
(``?token=``/``?apikey=``/``?_sid=``/``?pass=``) never reach a tool result or a
log line. Every module that adopts this helper gets that redaction for free.

Module-specific response unwrapping (PBS's ``data`` unwrap, Technitium's status
check) stays in the calling module; this helper only owns the transport +
error-mapping shell.
"""

from typing import Any

import httpx
from fastmcp import Context

from lib.redact import redact_exception


async def service_request(
    ctx: Context,
    service_key: str,
    path: str,
    *,
    method: str = "GET",
    params: dict | None = None,
    json: dict | list | None = None,
    headers: dict | None = None,
    display_name: str | None = None,
) -> Any:
    """Issue an HTTP request to a configured upstream and return parsed JSON.

    ``service_key`` names the client in ``ctx.lifespan_context``. On failure the
    function returns the conventional error dict (``{"error", "message"}`` plus
    ``"status"`` for HTTP errors) rather than raising, with any URL query string
    redacted out of the message.
    """
    name = display_name or service_key.capitalize()
    client = ctx.lifespan_context[service_key]
    try:
        if method == "GET":
            kw = {"params": params}
            if headers is not None:
                kw["headers"] = headers
            resp = await client.get(path, **kw)
        elif method == "POST":
            resp = await client.post(path, params=params, json=json)
        elif method == "PUT":
            resp = await client.put(path, params=params, json=json)
        else:
            resp = await client.request(method, path, params=params, json=json)
        resp.raise_for_status()
        return resp.json()
    except httpx.TimeoutException:
        return {"error": "timeout", "message": f"{name} did not respond in time"}
    except httpx.HTTPStatusError as e:
        return {
            "error": "http_error",
            "status": e.response.status_code,
            "message": redact_exception(e),
        }
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}
    except ValueError:
        # A 200 response whose body is not JSON (e.g. a reverse proxy returning
        # an HTML error page) makes resp.json() raise json.JSONDecodeError (a
        # ValueError subclass). Map it to the standard error dict instead of
        # letting it propagate out of the tool.
        return {
            "error": "invalid_response",
            "message": f"{name} returned a non-JSON response",
        }
