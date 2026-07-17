"""Content sync: fetch docs, stacks, and vault notes from Gitea repos.

Split from the former monolithic lib/refresh.py (the other half is
lib/refresh_registries.py).
"""

import asyncio
import logging
import time

import config

_logger = logging.getLogger(__name__)


async def _fetch_gitea_directory(
    client, owner: str, repo: str, path: str = "", ref: str = "master"
) -> list[dict]:
    """List files in a Gitea repo directory."""
    resp = await client.get(
        f"/api/v1/repos/{owner}/{repo}/contents/{path}",
        params={"ref": ref},
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        return [data]
    return data


async def _fetch_gitea_file_raw(
    client, owner: str, repo: str, filepath: str, ref: str = "master"
) -> str:
    """Fetch raw file content from Gitea."""
    resp = await client.get(
        f"/api/v1/repos/{owner}/{repo}/raw/{filepath}",
        params={"ref": ref},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.text


async def _fetch_stacks_from_gitea(clients: dict) -> dict:
    """Fetch Docker compose stacks from Gitea stacks repo."""
    client = clients.get("gitea")
    if client is None:
        return {}
    try:
        owner, repo = config.STACKS_REPO.split("/", 1)
        ref = config.STACKS_BRANCH
        entries = await _fetch_gitea_directory(client, owner, repo, ref=ref)
        dirs = [e for e in entries if e.get("type") == "dir"]

        async def _fetch_compose(entry_name):
            for filename in ("docker-compose.yaml", "docker-compose.yml"):
                try:
                    content = await _fetch_gitea_file_raw(
                        client, owner, repo, f"{entry_name}/{filename}", ref=ref
                    )
                    return entry_name, content
                except Exception:
                    continue
            return entry_name, None

        results = await asyncio.gather(*[_fetch_compose(d["name"]) for d in dirs[:30]])
        return {name: content for name, content in results if content is not None}
    except Exception as e:
        _logger.warning("Stacks fetch from Gitea failed: %s", e)
        return {}


async def _fetch_docs_from_gitea(clients: dict) -> tuple[dict, dict]:
    """Fetch docs and vault from Gitea docs repo."""
    client = clients.get("gitea")
    if client is None:
        return {}, {}
    try:
        owner, repo = config.DOCS_REPO.split("/", 1)
        ref = config.DOCS_BRANCH
        docs_index = {}
        vault_index = {}

        # Fetch docs/ directory
        try:
            doc_entries = await _fetch_gitea_directory(
                client, owner, repo, "docs", ref=ref
            )
            md_entries = [e for e in doc_entries if e.get("name", "").endswith(".md")]

            async def _fetch_doc(entry):
                content = await _fetch_gitea_file_raw(
                    client, owner, repo, f"docs/{entry['name']}", ref=ref
                )
                return entry["name"], content

            doc_results = await asyncio.gather(
                *[_fetch_doc(e) for e in md_entries[:50]]
            )
            for name, content in doc_results:
                docs_index[name] = {
                    "path": f"gitea://{owner}/{repo}/docs/{name}",
                    "content": content,
                    "sections": config.parse_sections(content),
                }
        except Exception as e:
            _logger.warning("Docs directory fetch failed: %s", e)

        # Fetch vault/ directory
        try:
            vault_entries = await _fetch_gitea_directory(
                client, owner, repo, "vault", ref=ref
            )
            vault_md = [e for e in vault_entries if e.get("name", "").endswith(".md")]

            async def _fetch_vault(entry):
                content = await _fetch_gitea_file_raw(
                    client, owner, repo, f"vault/{entry['name']}", ref=ref
                )
                return entry["name"], content

            vault_results = await asyncio.gather(
                *[_fetch_vault(e) for e in vault_md[:50]]
            )
            for name, content in vault_results:
                vault_index[name] = {
                    "path": f"gitea://{owner}/{repo}/vault/{name}",
                    "content": content,
                    "sections": config.parse_sections(content),
                }
        except Exception as e:
            _logger.warning("Vault directory fetch failed: %s", e)

        return docs_index, vault_index
    except Exception as e:
        _logger.warning("Docs/vault fetch from Gitea failed: %s", e)
        return {}, {}


async def refresh_docs_impl(clients: dict) -> dict:
    """Refresh docs, stacks, and vault from Gitea repos. Returns diff summary."""
    old_docs_count = len(config.DOCS_INDEX)
    old_stacks_count = len(config.STACKS_INDEX)
    old_vault_count = len(config.VAULT_INDEX)

    # Fetch stacks from stacks repo
    new_stacks = await _fetch_stacks_from_gitea(clients)
    if new_stacks:  # Only update if we got data (don't wipe on Gitea failure)
        # In-place mutation (like DOCS_INDEX/VAULT_INDEX and refresh_registries)
        # so any code holding a reference to config.STACKS_INDEX sees the update
        # instead of a stale rebind.
        config.STACKS_INDEX.clear()
        config.STACKS_INDEX.update(new_stacks)

    # Fetch docs and vault from docs repo
    new_docs, new_vault = await _fetch_docs_from_gitea(clients)
    if new_docs:
        config.DOCS_INDEX.clear()
        config.DOCS_INDEX.update(new_docs)
    if new_vault:
        config.VAULT_INDEX.clear()
        config.VAULT_INDEX.update(new_vault)

    # Update timestamp
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    config.REFRESH_TIMESTAMPS["docs"] = now

    diff = {
        "stacks_updated": len(new_stacks),
        "stacks_previous": old_stacks_count,
        "docs_updated": len(new_docs),
        "docs_previous": old_docs_count,
        "vault_updated": len(new_vault),
        "vault_previous": old_vault_count,
    }
    _logger.info("Doc refresh complete: %s", diff)
    return diff
