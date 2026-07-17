"""Gitea DevOps tools for homelab MCP server."""

import asyncio
from typing import Annotated, Any

from fastmcp import Context

import config
from lib.gitea import paginate_repos
from lib.http import service_request
from lib.meta import build_meta


def register(mcp):
    """Register Gitea tools. Skips if credentials are not configured."""
    if not config.GITEA_URL or not config.GITEA_TOKEN:
        return

    async def _get(ctx: Context, path: str, params: dict | None = None) -> Any:
        """Execute GET request against Gitea API."""
        return await service_request(
            ctx, "gitea", path, params=params, display_name="Gitea"
        )

    async def _get_all_repos(ctx: Context) -> list | dict:
        """Fetch all repos accessible to the token, including org repos, with pagination."""
        return await paginate_repos(
            lambda params: _get(ctx, "/api/v1/repos/search", params)
        )

    # ---- Tool 1: get_gitea_repos (GITA-01) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_gitea_repos(ctx: Context) -> dict:
        """List all Gitea repositories accessible to the configured token, including organization repos."""
        repos = await _get_all_repos(ctx)
        if isinstance(repos, dict) and "error" in repos:
            return repos

        repos_list = [
            {
                "name": r.get("name", ""),
                "full_name": r.get("full_name", ""),
                "description": r.get("description", ""),
                "url": r.get("html_url", ""),
                "stars": r.get("stars_count", 0),
                "forks": r.get("forks_count", 0),
                "open_issues": r.get("open_issues_count", 0),
                "updated": r.get("updated_at", ""),
            }
            for r in repos
        ]
        return {
            "repos": repos_list,
            "repo_count": len(repos_list),
            "_meta": build_meta("gitea"),
        }

    # ---- Tool 2: get_gitea_pull_requests (GITA-02) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_gitea_pull_requests(
        ctx: Context,
        repo: Annotated[
            str | None,
            "Repository full name (owner/repo). If omitted, returns open PRs across all repos.",
        ] = None,
    ) -> dict:
        """List open pull requests from Gitea. Shows all PRs including Renovate dependency updates. Provide repo name (e.g. 'org/repo') for a single repo, or omit to search all repos."""

        def _format_pr(pr: dict, full_name: str) -> dict:
            return {
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "repo": full_name,
                "author": pr.get("user", {}).get("login", ""),
                "created": pr.get("created_at", ""),
                "updated": pr.get("updated_at", ""),
                "labels": [lbl.get("name", "") for lbl in pr.get("labels", []) or []],
            }

        if repo:
            prs = await _get(
                ctx, f"/api/v1/repos/{repo}/pulls", {"state": "open", "limit": 50}
            )
            if isinstance(prs, dict) and "error" in prs:
                return prs
            if isinstance(prs, list):
                all_prs = [_format_pr(pr, repo) for pr in prs]
            else:
                all_prs = []
            return {
                "pull_requests": all_prs,
                "pr_count": len(all_prs),
                "_meta": build_meta("gitea"),
            }

        # Cross-repo aggregation
        repos_list = await _get_all_repos(ctx)
        if isinstance(repos_list, dict) and "error" in repos_list:
            return repos_list

        # Cap concurrency so 100+ repos don't fan out 100+ simultaneous requests
        # against a small Gitea instance (mirrors healthchecks.py's Semaphore(10)).
        sem = asyncio.Semaphore(10)

        async def fetch_prs(r):
            full_name = r.get("full_name", "")
            async with sem:
                result = await _get(
                    ctx,
                    f"/api/v1/repos/{full_name}/pulls",
                    {"state": "open", "limit": 50},
                )
            return (full_name, result)

        results = await asyncio.gather(
            *[fetch_prs(r) for r in repos_list], return_exceptions=True
        )

        all_prs = []
        for result in results:
            if isinstance(result, BaseException):
                continue
            full_name, prs = result
            if isinstance(prs, dict) and "error" in prs:
                continue
            if isinstance(prs, list):
                all_prs.extend(_format_pr(pr, full_name) for pr in prs)

        return {
            "pull_requests": all_prs,
            "pr_count": len(all_prs),
            "_meta": build_meta("gitea"),
        }

    # ---- Tool 3: get_gitea_ci_runs (GITA-03) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_gitea_ci_runs(
        ctx: Context,
        limit: Annotated[int, "Maximum number of runs to return"] = 20,
    ) -> dict:
        """List recent CI/CD workflow runs across all Gitea repositories. Returns the most recent runs sorted by creation time."""
        repos_list = await _get_all_repos(ctx)
        if isinstance(repos_list, dict) and "error" in repos_list:
            return repos_list

        # Fetch up to `limit` runs per repo (capped) so a busy repo's recent
        # runs are not dropped by a hardcoded per-repo cap of 5 before the global
        # sort/slice below.
        per_repo = min(max(limit, 1), 50)

        # Cap concurrency (see get_gitea_pull_requests).
        sem = asyncio.Semaphore(10)

        async def fetch_runs(r):
            full_name = r.get("full_name", "")
            async with sem:
                result = await _get(
                    ctx, f"/api/v1/repos/{full_name}/actions/runs", {"limit": per_repo}
                )
            return (full_name, result)

        results = await asyncio.gather(
            *[fetch_runs(r) for r in repos_list], return_exceptions=True
        )

        all_runs = []
        for result in results:
            if isinstance(result, BaseException):
                continue
            full_name, data = result
            if isinstance(data, dict) and "error" in data:
                # Graceful 403 handling: skip repos with permission denied
                continue
            runs = data.get("workflow_runs", []) if isinstance(data, dict) else []
            for run in runs:
                all_runs.append(
                    {
                        "repo": full_name,
                        "run_id": run.get("id"),
                        "name": run.get("name", ""),
                        "status": run.get("status", ""),
                        "conclusion": run.get("conclusion", ""),
                        "created": run.get("created_at", ""),
                        "updated": run.get("updated_at", ""),
                        "event": run.get("event", ""),
                    }
                )

        # Sort by created_at desc
        all_runs.sort(key=lambda r: r.get("created", ""), reverse=True)
        limited = all_runs[:limit]

        return {
            "runs": limited,
            "run_count": len(limited),
            "_meta": build_meta("gitea"),
        }
