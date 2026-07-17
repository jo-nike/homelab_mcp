"""On-demand knowledge refresh tools for homelab MCP server."""

from fastmcp import Context

from lib.audit import audit_log
from lib.meta import build_meta
from lib.refresh_content import refresh_docs_impl
from lib.refresh_registries import refresh_registries_impl

# Both refresh tools reload in-memory registries/docs from live sources. They
# mutate server state (readOnlyHint False) but are non-destructive and safely
# repeatable with the same args (destructiveHint False, idempotentHint True).
# They carry no dry_run -- reloading a cache has no preview -- but are still
# audit-logged like every other write (D9).
_REFRESH_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def register(mcp):
    """Register refresh tools. Always available (uses existing lifespan clients)."""

    @mcp.tool(annotations=_REFRESH_ANNOTATIONS)
    async def refresh_registries(ctx: Context) -> dict:
        """Force an immediate refresh of service and host registries from all live API sources (Portainer, Proxmox, DNS, NPM, Scanopy, WireGuard, Healthchecks, Gitea). Returns a summary of what changed."""
        try:
            diff = await refresh_registries_impl(ctx.lifespan_context)
            await audit_log(
                ctx, action="refresh_registries", target="registries", result="success"
            )
            return {
                "action": "refresh_registries",
                "result": "success",
                "diff": diff,
                "_meta": build_meta("registries"),
            }
        except Exception as e:
            await audit_log(
                ctx, action="refresh_registries", target="registries", result="failure"
            )
            return {
                "action": "refresh_registries",
                "error": "refresh_failed",
                "message": str(e),
                "_meta": build_meta("registries", confidence="low"),
            }

    @mcp.tool(annotations=_REFRESH_ANNOTATIONS)
    async def refresh_docs(ctx: Context) -> dict:
        """Force an immediate refresh of documentation, Docker stacks, and vault notes from Gitea repositories. Returns a summary of what changed."""
        try:
            diff = await refresh_docs_impl(ctx.lifespan_context)
            await audit_log(ctx, action="refresh_docs", target="docs", result="success")
            return {
                "action": "refresh_docs",
                "result": "success",
                "diff": diff,
                "_meta": build_meta("docs"),
            }
        except Exception as e:
            await audit_log(ctx, action="refresh_docs", target="docs", result="failure")
            return {
                "action": "refresh_docs",
                "error": "refresh_failed",
                "message": str(e),
                "_meta": build_meta("docs", confidence="low"),
            }
