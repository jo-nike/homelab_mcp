"""Shared Gitea repo pagination.

``tools/gitea`` (unbounded loop) and ``lib/refresh`` (capped at 5 pages) both
walked ``/api/v1/repos/search`` with ``limit=50`` and had drifted on the page
cap. This owns one walk with an explicit cap.
"""


async def paginate_repos(fetch, max_pages: int | None = 20) -> list | dict:
    """Walk ``/api/v1/repos/search`` pages via ``fetch({"limit": 50, "page": n})``.

    ``fetch`` is an async callable returning the parsed page (a dict carrying a
    ``data`` list, or a bare list), an error dict, or ``None``. Returns the
    combined raw repo list, or the error dict if one is encountered. Stops after
    ``max_pages`` (``None`` = unbounded).
    """
    repos: list = []
    page = 1
    while max_pages is None or page <= max_pages:
        result = await fetch({"limit": 50, "page": page})
        if isinstance(result, dict) and "error" in result:
            return result
        if result is None:
            break
        items = result.get("data", result) if isinstance(result, dict) else result
        if not items:
            break
        repos.extend(items)
        if len(items) < 50:
            break
        page += 1
    return repos
