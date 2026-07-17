"""llama-server (llama.cpp) inference tools for homelab MCP server."""

import asyncio
from typing import Any

from fastmcp import Context

import config
from lib.http import service_request
from lib.meta import build_meta


def register(mcp):
    """Register llama-server tools. Skips if not configured."""
    if not config.LLAMA_SERVER_URL:
        return

    async def _get(ctx, path, params=None):
        return await service_request(
            ctx, "llama_server", path, params=params, display_name="llama-server"
        )

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_llama_status(ctx: Context) -> dict:
        """Get llama-server inference status: health, available models, loaded model slots, and context usage on Beast."""
        health_data, props_data = await asyncio.gather(
            _get(ctx, "/health"),
            _get(ctx, "/props"),
        )

        # Health section
        health = {}
        if isinstance(health_data, dict) and "error" not in health_data:
            health = {"status": health_data.get("status", "unknown")}
        else:
            health = {"status": "error", "detail": health_data}

        health_status = health.get("status", "unknown")

        # Detect router vs single-model mode
        is_router = (
            isinstance(props_data, dict)
            and "error" not in props_data
            and props_data.get("role") == "router"
        )

        if is_router:
            return await _build_router_status(ctx, health, health_status, props_data)
        else:
            return await _build_single_model_status(
                ctx, health, health_status, props_data
            )

    async def _build_router_status(ctx, health, health_status, props_data):
        """Build status for router-mode llama-server (multi-model)."""
        models_data = await _get(ctx, "/models")

        models = []
        loaded_models = []

        if isinstance(models_data, dict) and "data" in models_data:
            for m in models_data["data"]:
                model_id = m.get("id", "unknown")
                status_info = m.get("status", {})
                state = status_info.get("value", "unknown")

                model_entry = {
                    "id": model_id,
                    "state": state,
                }

                if state == "loaded":
                    loaded_models.append(model_id)

                # Extract key config from args if available. Values can be
                # non-numeric (e.g. '--n-gpu-layers auto'), so skip the field
                # rather than raising ValueError out of the tool.
                args = status_info.get("args", [])
                for i, arg in enumerate(args):
                    if arg == "--ctx-size" and i + 1 < len(args):
                        try:
                            model_entry["n_ctx"] = int(args[i + 1])
                        except (ValueError, TypeError):
                            pass
                    elif arg == "--n-gpu-layers" and i + 1 < len(args):
                        try:
                            model_entry["n_gpu_layers"] = int(args[i + 1])
                        except (ValueError, TypeError):
                            pass

                if state == "failed" or status_info.get("failed"):
                    model_entry["state"] = "failed"

                models.append(model_entry)

        # Fetch slots for each loaded model
        slots_by_model = {}
        if loaded_models:
            slot_results = await asyncio.gather(
                *[_get(ctx, "/slots", params={"model": mid}) for mid in loaded_models]
            )
            for mid, slot_data in zip(loaded_models, slot_results, strict=False):
                if isinstance(slot_data, list):
                    slots_by_model[mid] = _parse_slots(slot_data)

        # Build summary
        total_models = len(models)
        loaded_count = len(loaded_models)
        parts = [f"llama-server {health_status} (router)"]
        parts.append(f"{loaded_count}/{total_models} models loaded")

        if loaded_models:
            parts.append(f"active: {', '.join(loaded_models)}")

        for mid, slot_info in slots_by_model.items():
            if slot_info["processing"] > 0:
                parts.append(
                    f"{mid}: {slot_info['processing']}/{slot_info['total']} slots active"
                )

        summary = ", ".join(parts)
        max_instances = props_data.get("max_instances", "unknown")

        return {
            "summary": summary,
            "health": health,
            "mode": "router",
            "max_instances": max_instances,
            "models": models,
            "loaded_models": loaded_models,
            "slots": slots_by_model,
            "_meta": build_meta("llama-server"),
        }

    async def _build_single_model_status(ctx, health, health_status, props_data):
        """Build status for single-model llama-server (original behavior)."""
        slots_data = await _get(ctx, "/slots")

        # Props section -- extract model name
        model_name = "unknown"
        if isinstance(props_data, dict) and "error" not in props_data:
            model_name = (
                props_data.get("default_generation_settings", {}).get("model", "")
                or props_data.get("model", "")
                or "unknown"
            )
            if "/" in model_name:
                model_name = model_name.rsplit("/", 1)[-1]
            for ext in (".gguf", ".bin"):
                if model_name.endswith(ext):
                    model_name = model_name[: -len(ext)]

        # Slots section
        slots_info: dict[str, Any] = (
            _parse_slots(slots_data)
            if isinstance(slots_data, list)
            else (
                slots_data
                if isinstance(slots_data, dict) and "error" in slots_data
                else {"total": 0, "idle": 0, "processing": 0, "details": []}
            )
        )

        # Context usage
        context_usage_percent = 0.0
        if isinstance(slots_data, list):
            processing_ctx_pcts = []
            for slot in slots_data:
                if slot.get("state", 0) == 1:
                    n_ctx = slot.get("n_ctx", 0)
                    tokens_evaluated = slot.get("tokens_evaluated", 0)
                    if n_ctx > 0:
                        processing_ctx_pcts.append(
                            tokens_evaluated / max(n_ctx, 1) * 100
                        )
            if processing_ctx_pcts:
                context_usage_percent = round(
                    sum(processing_ctx_pcts) / len(processing_ctx_pcts), 1
                )

        # Build summary
        total = slots_info.get("total", 0) if isinstance(slots_info, dict) else 0
        processing = (
            slots_info.get("processing", 0) if isinstance(slots_info, dict) else 0
        )

        parts = [f"llama-server {health_status}"]
        if model_name != "unknown":
            parts.append(f"model {model_name} loaded")
        if total > 0:
            parts.append(f"{processing}/{total} slots active")
            if context_usage_percent > 0:
                parts.append(f"{context_usage_percent}% context used")

        summary = ", ".join(parts)

        return {
            "summary": summary,
            "health": health,
            "mode": "single",
            "model": model_name,
            "slots": slots_info,
            "context_usage_percent": context_usage_percent,
            "_meta": build_meta("llama-server"),
        }

    def _parse_slots(slots_data) -> dict[str, Any]:
        """Parse a slots array into a summary dict."""
        info: dict[str, Any] = {"total": 0, "idle": 0, "processing": 0, "details": []}

        if not isinstance(slots_data, list):
            return info

        info["total"] = len(slots_data)

        for slot in slots_data:
            state_val = slot.get("state", 0)
            # Router mode uses is_processing bool; single mode uses state int
            if isinstance(state_val, bool):
                is_processing = state_val
            else:
                is_processing = state_val == 1

            # Also check is_processing field directly (newer llama.cpp)
            if "is_processing" in slot:
                is_processing = slot["is_processing"]

            state_str = "processing" if is_processing else "idle"

            if is_processing:
                info["processing"] += 1
            else:
                info["idle"] += 1

            n_ctx = slot.get("n_ctx", 0)
            info["details"].append(
                {
                    "id": slot.get("id", 0),
                    "state": state_str,
                    "n_ctx": n_ctx,
                    "tokens_predicted": slot.get("tokens_predicted", 0),
                    "tokens_evaluated": slot.get("tokens_evaluated", 0),
                }
            )

        return info
