"""MCP resource definitions for homelab docs, stacks, and vault notes."""

from pathlib import Path

import config


def _contained(base: Path, candidate: Path) -> bool:
    """True if candidate resolves to a path inside (or equal to) base.

    Guards the filesystem fallbacks against path traversal: the FastMCP
    resource template captures {path}/{name} on the still-encoded URI and then
    unquotes it, so a request like homelab://vault/..%2F..%2F.env decodes to
    '../../.env'. Without this check that would escape the vault dir and read
    the repo .env (or /proc/self/environ in the container).
    """
    try:
        resolved_base = base.resolve()
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved == resolved_base or resolved_base in resolved.parents


def _lookup(
    entity, name, index, index_keys, index_get, base, path_candidates, available
):
    """Shared resource resolution: index lookup, then contained filesystem
    fallback, then a sorted 'not found. Available: ...' message.

    ``index_keys`` are tried in order against ``index`` (``index_get`` extracts
    the content from a hit); ``path_candidates`` are tried in order and read if
    they exist and resolve inside ``base``; ``available`` is a callable invoked
    only on the miss path to list known names.
    """
    for key in index_keys:
        if key in index:
            return index_get(index[key])
    for candidate in path_candidates:
        if candidate.exists() and _contained(base, candidate):
            # errors="replace" mirrors config.load_docs_index so a non-UTF-8 byte
            # returns content rather than raising UnicodeDecodeError.
            return candidate.read_text(errors="replace")
    return f"{entity} '{name}' not found. Available: {', '.join(available())}"


def register(mcp):
    """Register MCP resource handlers for docs, stacks, and vault notes."""

    @mcp.resource("homelab://docs/{name}")
    def get_doc(name: str) -> str:
        """Read a homelab documentation file."""
        lookup = name if name.endswith(".md") else f"{name}.md"
        bare = name[:-3] if name.endswith(".md") else name
        base = Path(config.DOCS_DIR)

        def available():
            names = list(config.DOCS_INDEX.keys())
            if base.exists():
                names += [f.stem for f in base.glob("*.md")]
            return sorted(set(names))

        return _lookup(
            "Document",
            name,
            config.DOCS_INDEX,
            [lookup, bare],
            lambda v: v.get("content", ""),
            base,
            [base / lookup, base / bare],
            available,
        )

    @mcp.resource("homelab://stacks/{name}")
    def get_stack(name: str) -> str:
        """Read a Docker compose stack file."""
        base = Path(config.STACKS_DIR)

        def available():
            names = list(config.STACKS_INDEX.keys())
            if base.exists():
                names += [d.name for d in base.iterdir() if d.is_dir()]
            return sorted(set(names))

        return _lookup(
            "Stack",
            name,
            config.STACKS_INDEX,
            [name],
            lambda v: v,
            base,
            [
                base / name / "docker-compose.yaml",
                base / name / "docker-compose.yml",
                base / name,
            ],
            available,
        )

    @mcp.resource("homelab://vault/{path}")
    def get_vault_note(path: str) -> str:
        """Read an Obsidian vault note about the homelab."""
        lookup = path if path.endswith(".md") else f"{path}.md"
        bare = path[:-3] if path.endswith(".md") else path
        base = Path(config.VAULT_DIR)

        def available():
            names = list(config.VAULT_INDEX.keys())
            if base.exists():
                names += [f.name for f in base.glob("*.md")]
            return sorted(set(names))

        return _lookup(
            "Vault note",
            path,
            config.VAULT_INDEX,
            [lookup, bare],
            lambda v: v.get("content", ""),
            base,
            [base / path, base / f"{path}.md"],
            available,
        )
