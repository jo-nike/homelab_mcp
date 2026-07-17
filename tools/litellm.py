"""LiteLLM proxy tools for homelab MCP server."""

import asyncio

from fastmcp import Context

import config
from lib.http import service_request
from lib.meta import build_meta


def register(mcp):
    """Register LiteLLM tools. Skips if not configured."""
    if not (config.LITELLM_URL and config.LITELLM_API_KEY):
        return

    async def _get(ctx, path, params=None):
        return await service_request(
            ctx, "litellm", path, params=params, display_name="LiteLLM"
        )

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_litellm_status(ctx: Context) -> dict:
        """Get LiteLLM proxy status: health, available models, and routing info. Does not trigger expensive model health checks."""
        health_data, models_data = await asyncio.gather(
            _get(ctx, "/health/readiness"),
            _get(ctx, "/v1/models"),
        )

        # Build health section (partial results on error)
        health = {}
        if isinstance(health_data, dict) and "error" not in health_data:
            health = {
                "status": health_data.get("status", "unknown"),
                "litellm_version": health_data.get("litellm_version", "unknown"),
            }
        else:
            health = {"status": "error", "detail": health_data}

        # Build models section (partial results on error). Keep `models` a stable
        # list type; surface any fetch error in a dedicated `models_error` key
        # rather than switching `models` from list to dict.
        models = []
        model_count = 0
        models_error = None
        if isinstance(models_data, dict) and "error" not in models_data:
            for m in models_data.get("data", []):
                # /v1/models (OpenAI-compatible) returns only id/object/created/
                # owned_by. litellm_params/max_tokens live on LiteLLM's
                # /model/info endpoint, so nothing more can be extracted here.
                models.append({"model_name": m.get("id", "unknown")})
            model_count = len(models)
        elif isinstance(models_data, dict) and "error" in models_data:
            models_error = models_data

        # Build summary
        status_str = health.get("status", "unknown")
        if model_count > 0:
            summary = f"LiteLLM {status_str}, {model_count} model{'s' if model_count != 1 else ''} available"
        else:
            summary = f"LiteLLM {status_str}, models unavailable"

        result = {
            "summary": summary,
            "health": health,
            "models": models,
            "model_count": model_count,
            "_meta": build_meta("litellm"),
        }
        if models_error is not None:
            result["models_error"] = models_error
        return result
