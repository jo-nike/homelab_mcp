"""Gitea DevOps tools for homelab MCP server."""

import asyncio
from typing import Annotated, Any

import httpx
from fastmcp import Context

import config
from lib.audit import audit_log
from lib.gitea import paginate_repos, parse_renovate_body
from lib.http import service_request
from lib.meta import build_meta
from lib.redact import redact_exception

# CI states that block an automatic merge (not green). "success"/"warning"/
# "none"/"unknown" are allowed; the merge itself is the final arbiter.
_CI_BLOCKING = {"pending", "failure", "error"}


def _is_renovate(pr: dict) -> bool:
    """True if a PR looks like a Renovate dependency update (by author or label)."""
    author = (pr.get("user") or {}).get("login", "").lower()
    if "renovate" in author:
        return True
    labels = {(lbl.get("name") or "").lower() for lbl in pr.get("labels") or []}
    return bool(labels & {"dependencies", "renovate"})


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

    async def _ci_status(ctx: Context, repo: str, sha: str) -> str:
        """Combined commit-status state for a PR head (``none`` if no checks).

        Gitea reports ``state="pending"`` with ``total_count=0`` for a commit that
        has *no* status checks at all (verified against docker-stacks PRs). Treat
        that as ``none`` so PRs on a repo without CI stay mergeable; a real
        ``pending`` (with actual statuses) still blocks.
        """
        if not sha:
            return "none"
        result = await _get(ctx, f"/api/v1/repos/{repo}/commits/{sha}/status")
        if isinstance(result, dict) and "error" in result:
            return "unknown"
        if not isinstance(result, dict):
            return "none"
        if not (result.get("total_count") or result.get("statuses")):
            return "none"
        return (result.get("state") or "").strip() or "none"

    async def _merge_pr(ctx: Context, repo: str, pr_number: int) -> dict:
        """Merge a PR with a merge commit. Gitea returns an empty 200 on success,
        so this checks the status directly rather than going through
        ``service_request`` (which would mis-map the empty body to
        ``invalid_response``). Mirrors lib.http's error ladder otherwise."""
        client = ctx.lifespan_context["gitea"]
        path = f"/api/v1/repos/{repo}/pulls/{pr_number}/merge"
        try:
            resp = await client.post(path, json={"Do": "merge"})
            resp.raise_for_status()
        except httpx.TimeoutException:
            return {"error": "timeout", "message": "Gitea did not respond in time"}
        except httpx.HTTPStatusError as e:
            return {
                "error": "http_error",
                "status": e.response.status_code,
                "message": redact_exception(e),
            }
        except httpx.HTTPError as e:
            return {"error": "connection_error", "message": redact_exception(e)}
        return {"merged": True}

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

    # ---- Tool 4: get_image_updates (GITA-04) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_image_updates(
        ctx: Context,
        repo: Annotated[
            str | None,
            "Repository full name (owner/repo). Defaults to the docker-stacks repo; "
            "pass another repo, or '*' to scan every repo.",
        ] = None,
    ) -> dict:
        """List pending container image updates (Renovate PRs) with current → proposed version.

        Reads the open Renovate dependency PRs and returns, per update, the package,
        semver bump type (major/minor/patch), the version now → proposed, any release
        notes (when Renovate embeds them), and the PR's CI status. This is the review
        view for apply_image_update, not the running-container tools.
        """
        target = repo or config.STACKS_REPO

        async def _repo_prs(full_name: str) -> list | dict:
            return await _get(
                ctx, f"/api/v1/repos/{full_name}/pulls", {"state": "open", "limit": 50}
            )

        pairs: list[tuple[str, dict]] = []
        if target == "*":
            repos_list = await _get_all_repos(ctx)
            if isinstance(repos_list, dict) and "error" in repos_list:
                return repos_list
            sem = asyncio.Semaphore(10)

            async def fetch(r):
                full_name = r.get("full_name", "")
                async with sem:
                    result = await _repo_prs(full_name)
                return (full_name, result)

            results = await asyncio.gather(
                *[fetch(r) for r in repos_list], return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    continue
                full_name, prs = result
                if isinstance(prs, dict) and "error" in prs:
                    continue
                if isinstance(prs, list):
                    pairs.extend((full_name, pr) for pr in prs)
        else:
            prs = await _repo_prs(target)
            if isinstance(prs, dict) and "error" in prs:
                return prs
            if isinstance(prs, list):
                pairs = [(target, pr) for pr in prs]

        renovate = [(full, pr) for (full, pr) in pairs if _is_renovate(pr)]

        # CI status per PR, concurrency-capped (see get_gitea_pull_requests).
        sem2 = asyncio.Semaphore(10)

        async def _enrich(full_name: str, pr: dict) -> dict:
            parsed = parse_renovate_body(pr.get("body") or "")
            sha = (pr.get("head") or {}).get("sha", "")
            async with sem2:
                ci = await _ci_status(ctx, full_name, sha)
            return {
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "repo": full_name,
                "url": pr.get("html_url", ""),
                "author": pr.get("user", {}).get("login", ""),
                "created": pr.get("created_at", ""),
                "updated": pr.get("updated_at", ""),
                "updates": parsed["updates"],
                "release_notes": parsed["release_notes"],
                "ci_status": ci,
            }

        image_updates = await asyncio.gather(
            *[_enrich(full, pr) for (full, pr) in renovate]
        )
        return {
            "image_updates": list(image_updates),
            "update_count": len(image_updates),
            "_meta": build_meta("gitea"),
        }

    # ---- Tool 5: apply_image_update (GITA-05, WRITE) ----

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "openWorldHint": False,
        }
    )
    async def apply_image_update(
        ctx: Context,
        pr_number: Annotated[int, "Pull request number of the image update to apply"],
        repo: Annotated[
            str | None,
            "Repository full name (owner/repo); defaults to the docker-stacks repo",
        ] = None,
        force: Annotated[bool, "Merge even if CI checks are not passing"] = False,
        dry_run: Annotated[bool, "Preview what would happen without executing"] = False,
    ) -> dict:
        """Approve and merge a pending container image update PR (triggers deployment). Use dry_run=True to preview.

        Merges the Renovate PR with a merge commit. Refuses if the PR's CI is not
        passing (override with force=True) or if it has merge conflicts. Use
        get_image_updates first to see what is pending.
        """
        target = repo or config.STACKS_REPO
        action = "apply_image_update"
        tgt = f"{target}#{pr_number}"
        params = {"repo": target, "pr_number": pr_number}

        pr = await _get(ctx, f"/api/v1/repos/{target}/pulls/{pr_number}")
        if isinstance(pr, dict) and "error" in pr:
            await audit_log(
                ctx,
                action=action,
                target=tgt,
                params=params,
                result="dry_run" if dry_run else "failure",
                dry_run=dry_run,
            )
            return pr

        title = pr.get("title", "")
        merged = pr.get("merged", False)
        state = pr.get("state", "")
        mergeable = pr.get("mergeable")
        sha = (pr.get("head") or {}).get("sha", "")
        updates = parse_renovate_body(pr.get("body") or "")["updates"]
        ci = await _ci_status(ctx, target, sha)
        ci_blocking = ci in _CI_BLOCKING

        def _summary(prefix: str) -> str:
            if updates:
                u = updates[0]
                desc = " ".join(
                    part
                    for part in (
                        u.get("package"),
                        f"{u.get('current')}→{u.get('proposed')}",
                    )
                    if part
                )
            else:
                desc = title
            return f"{prefix} PR #{pr_number} ({desc}) — CI {ci}"

        if dry_run:
            would_merge = (
                not merged
                and state == "open"
                and mergeable is not False
                and (not ci_blocking or force)
            )
            await audit_log(
                ctx,
                action=action,
                target=tgt,
                params=params,
                result="dry_run",
                dry_run=True,
            )
            return {
                "dry_run": True,
                "action": action,
                "repo": target,
                "pr_number": pr_number,
                "title": title,
                "updates": updates,
                "ci_status": ci,
                "mergeable": mergeable,
                "would_merge": would_merge,
                "summary": _summary("Would merge"),
            }

        # Guards (real run). An out-of-date-but-not-conflicting branch is fine:
        # Gitea's merge folds the base in via the merge commit.
        if merged or state != "open":
            await audit_log(
                ctx, action=action, target=tgt, params=params, result="failure"
            )
            return {
                "error": "not_open",
                "message": f"PR #{pr_number} is not open (state={state or 'unknown'}, merged={merged}).",
            }
        if mergeable is False:
            await audit_log(
                ctx, action=action, target=tgt, params=params, result="failure"
            )
            return {
                "error": "conflict",
                "message": f"PR #{pr_number} has merge conflicts; tick Renovate's rebase box on the PR, then retry.",
            }
        if ci_blocking and not force:
            await audit_log(
                ctx, action=action, target=tgt, params=params, result="failure"
            )
            return {
                "error": "ci_failing",
                "message": f"PR #{pr_number} CI is {ci}; not merging. Pass force=True to override.",
            }

        result = await _merge_pr(ctx, target, pr_number)
        if isinstance(result, dict) and "error" in result:
            await audit_log(
                ctx, action=action, target=tgt, params=params, result="failure"
            )
            return result

        await audit_log(ctx, action=action, target=tgt, params=params, result="success")
        return {
            "action": action,
            "repo": target,
            "pr_number": pr_number,
            "merged": True,
            "result": "success",
            "summary": _summary("Merged"),
            "_meta": build_meta("gitea"),
        }
