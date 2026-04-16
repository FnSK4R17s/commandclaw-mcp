"""Admin API for agent principal management.

Protected by a shared secret (X-Admin-Key header).
All mutations update both in-memory settings and the agents.json file on disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from commandclaw_mcp.config import AccessEntry, RateLimitConfig, Settings

logger = structlog.get_logger()

_AGENTS_PATH_DEFAULT = Path.home() / ".commandclaw" / "agents.json"


def _agents_path() -> Path:
    return Path(os.environ.get("COMMANDCLAW_AGENTS", str(_AGENTS_PATH_DEFAULT)))


def _persist(settings: Settings) -> None:
    """Write current access dict back to agents.json."""
    path = _agents_path()
    data = {
        agent_id: entry.model_dump(exclude_defaults=False)
        for agent_id, entry in settings.access.items()
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    logger.info("agents_json_persisted", path=str(path), count=len(data))


def _check_admin_key(request: Request, settings: Settings) -> JSONResponse | None:
    """Validate X-Admin-Key header. Returns error response or None if OK."""
    admin_key = settings.gateway.admin_key
    if not admin_key:
        return JSONResponse(
            {"error": "Admin API disabled — set COMMANDCLAW_GATEWAY__ADMIN_KEY"},
            status_code=503,
        )
    provided = request.headers.get("X-Admin-Key", "")
    if provided != admin_key:
        logger.warning("admin_auth_failed", path=request.url.path)
        return JSONResponse({"error": "Invalid admin key"}, status_code=403)
    return None


def create_admin_router(settings: Settings) -> APIRouter:
    """Create the /admin router for agent principal CRUD."""
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.get("/agents")
    async def list_agents(request: Request) -> JSONResponse:
        """List all enrolled agents."""
        err = _check_admin_key(request, settings)
        if err:
            return err

        agents: dict[str, Any] = {}
        for agent_id, entry in settings.access.items():
            agents[agent_id] = entry.model_dump()
        return JSONResponse(agents)

    @router.get("/agents/{agent_id}")
    async def get_agent(agent_id: str, request: Request) -> JSONResponse:
        """Get a single agent's config."""
        err = _check_admin_key(request, settings)
        if err:
            return err

        entry = settings.access.get(agent_id)
        if entry is None:
            return JSONResponse({"error": f"Agent not found: {agent_id}"}, status_code=404)
        return JSONResponse({"agent_id": agent_id, **entry.model_dump()})

    @router.post("/agents")
    async def create_agent(request: Request) -> JSONResponse:
        """Enroll a new agent.

        Body: {"agent_id": "...", "roles": [...], "tools": [...], "mode": "standard|admin"}
        """
        err = _check_admin_key(request, settings)
        if err:
            return err

        body = await request.json()
        agent_id = body.get("agent_id")
        if not agent_id:
            return JSONResponse({"error": "agent_id required"}, status_code=400)
        if agent_id in settings.access:
            return JSONResponse(
                {"error": f"Agent already exists: {agent_id}. Use PATCH to update."},
                status_code=409,
            )

        entry = AccessEntry(
            roles=body.get("roles", ["reader"]),
            tools=body.get("tools", []),
            mode=body.get("mode", "standard"),
            rate_limit=RateLimitConfig(
                requests_per_minute=body.get("rate_limit", {}).get("requests_per_minute", 60)
            ),
        )
        settings.access[agent_id] = entry
        _persist(settings)

        logger.info("agent_enrolled", agent_id=agent_id, mode=entry.mode, roles=entry.roles)
        return JSONResponse({"agent_id": agent_id, **entry.model_dump()}, status_code=201)

    @router.patch("/agents/{agent_id}")
    async def update_agent(agent_id: str, request: Request) -> JSONResponse:
        """Update an agent's config. Only provided fields are changed.

        Body: {"mode": "admin"} or {"roles": ["developer"], "tools": ["clock", "github"]}
        """
        err = _check_admin_key(request, settings)
        if err:
            return err

        entry = settings.access.get(agent_id)
        if entry is None:
            return JSONResponse({"error": f"Agent not found: {agent_id}"}, status_code=404)

        body = await request.json()
        if "roles" in body:
            entry.roles = body["roles"]
        if "tools" in body:
            entry.tools = body["tools"]
        if "mode" in body:
            if body["mode"] not in ("admin", "standard"):
                return JSONResponse(
                    {"error": "mode must be 'admin' or 'standard'"},
                    status_code=400,
                )
            entry.mode = body["mode"]
        if "rate_limit" in body:
            rpm = body["rate_limit"].get("requests_per_minute", entry.rate_limit.requests_per_minute)
            entry.rate_limit = RateLimitConfig(requests_per_minute=rpm)

        _persist(settings)

        logger.info("agent_updated", agent_id=agent_id, mode=entry.mode, roles=entry.roles)
        return JSONResponse({"agent_id": agent_id, **entry.model_dump()})

    @router.delete("/agents/{agent_id}")
    async def delete_agent(agent_id: str, request: Request) -> JSONResponse:
        """Remove an agent. Does not affect running containers."""
        err = _check_admin_key(request, settings)
        if err:
            return err

        if agent_id == "default":
            return JSONResponse(
                {"error": "Cannot delete the 'default' fallback entry"},
                status_code=400,
            )

        if agent_id not in settings.access:
            return JSONResponse({"error": f"Agent not found: {agent_id}"}, status_code=404)

        del settings.access[agent_id]
        _persist(settings)

        logger.info("agent_removed", agent_id=agent_id)
        return JSONResponse({"status": "removed", "agent_id": agent_id})

    return router
