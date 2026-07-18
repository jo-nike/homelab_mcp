"""Shared Gitea helpers: repo pagination and Renovate PR-body parsing.

``tools/gitea`` (unbounded loop) and ``lib/refresh`` (capped at 5 pages) both
walked ``/api/v1/repos/search`` with ``limit=50`` and had drifted on the page
cap. This owns one walk with an explicit cap.

``parse_renovate_body`` turns a Renovate PR body into structured version diffs
(current -> proposed) and, when present, the Release Notes section. It is a pure
string function so it is unit-testable without httpx.
"""

import re

# A ``current -> proposed`` version pair. Renovate writes the raw markdown as
# ``\`v1.6.4\` -> \`2.1.29\``` (Gitea renders the arrow as ``→``); handle
# both arrows, optional backticks, and an optional ``v`` prefix. Each version
# starts with a digit and runs until whitespace/backtick/pipe.
_VERSION_PAIR = re.compile(
    r"`?v?([0-9][^\s`|]*)`?\s*(?:->|=>|→)\s*`?v?([0-9][^\s`|]*)`?"
)
# A markdown link ``[text](url)`` -> ``text`` (used to clean the Package cell).
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# Renovate's severity keywords, matched against a table cell to find the
# ``Update`` column regardless of whether the table has 3 or 4 columns.
_UPDATE_TYPES = {
    "major",
    "minor",
    "patch",
    "pin",
    "digest",
    "rollback",
    "replacement",
    "bump",
    "lockfilemaintenance",
    "pindigest",
}


def _clean_package(cell: str) -> str:
    """Strip markdown links and a trailing ``(source)`` note from a Package cell."""
    cell = _MD_LINK.sub(r"\1", cell)
    cell = re.sub(r"\s*\(\s*source\s*\)\s*$", "", cell)
    return cell.strip()


def _extract_release_notes(body: str) -> str | None:
    """Return the Release Notes section text (truncated), or ``None`` if absent.

    Self-hosted Renovate without github.com credentials emits a "Release Notes
    retrieval ... skipped" notice instead of real notes; that is treated as
    absent. When notes are present they run until Renovate's Configuration
    footer, which bounds the capture.
    """
    if re.search(r"Release Notes retrieval.*skipped", body, re.IGNORECASE | re.DOTALL):
        return None
    m = re.search(r"Release Notes", body, re.IGNORECASE)
    if not m:
        return None
    section = body[m.start() :]
    # Renovate appends a Configuration section after the release notes; cut there.
    cut = re.search(r"#{2,3}\s*Configuration|\U0001f4c5\s*Schedule", section)
    if cut:
        section = section[: cut.start()]
    # Drop HTML chrome (<details>/<summary>/anchors) and collapse blank runs.
    text = re.sub(r"<[^>]+>", "", section)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return None
    if len(text) > 1500:
        text = text[:1500].rstrip() + "\n…(truncated)"
    return text


def parse_renovate_body(body: str) -> dict:
    """Parse a Renovate PR body into ``{updates, release_notes}``.

    ``updates`` is a list of ``{package, update_type, current, proposed}`` pulled
    from the PR's dependency table (empty if none match). ``release_notes`` is the
    trimmed Release Notes section or ``None``. Robust to missing/odd bodies.
    """
    updates: list[dict] = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # Skip header / separator rows.
        joined = " ".join(cells).lower()
        if not cells or "---" in joined or joined.strip() in ("package update change",):
            continue
        change = next(((c, m) for c in cells if (m := _VERSION_PAIR.search(c))), None)
        if change is None:
            continue
        _, match = change
        update_type = next((c for c in cells if c.lower() in _UPDATE_TYPES), None)
        updates.append(
            {
                "package": _clean_package(cells[0]),
                "update_type": update_type,
                "current": match.group(1),
                "proposed": match.group(2),
            }
        )
    return {"updates": updates, "release_notes": _extract_release_notes(body or "")}


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
