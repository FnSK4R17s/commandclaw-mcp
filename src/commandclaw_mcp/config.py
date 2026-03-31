"""Pydantic Settings from ~/.commandclaw/mcp.json + ~/.commandclaw/agents.json."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class GatewayConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8420
    key_rotation_interval_seconds: int = 3600
    overlap_window_seconds: int = 300
    encryption_seed: str = "CHANGE-ME-IN-PRODUCTION"
    encryption_seed_secondary: str | None = None  # For zero-downtime seed rotation
    require_tls_upstream: bool = True
    cors_allowed_origins: list[str] = Field(default_factory=lambda: ["*"])
    # Session backend: "redis" (default) or "token_encoded" (horizontal scaling)
    session_mode: str = "redis"
    # Max tools returned per tools/list call (Virtual MCP pattern, reduces LLM context)
    max_tools_per_session: int = 0  # 0 = unlimited, recommended: 8 for large deployments
    # Explicit override required to bind to 0.0.0.0 (non-loopback)
    allow_non_loopback: bool = False
    # Allow upstream servers on private IPs (Docker Compose / trusted networks)
    allow_private_upstream: bool = False


class ServerConfig(BaseModel):
    """An upstream MCP server — either stdio (command) or HTTP (url)."""

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None

    @model_validator(mode="after")
    def require_command_or_url(self) -> ServerConfig:
        if not self.command and not self.url:
            msg = "Server must have either 'command' (stdio) or 'url' (HTTP)"
            raise ValueError(msg)
        return self

    @property
    def is_stdio(self) -> bool:
        return self.command is not None


class RateLimitConfig(BaseModel):
    requests_per_minute: int = 60


class AccessEntry(BaseModel):
    roles: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    mode: str = Field(default="standard", description="Agent privilege mode: admin or standard")
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)


class RedisConfig(BaseModel):
    url: str = "redis://localhost:6379/0"


class CerbosConfig(BaseModel):
    host: str = "localhost"
    port: int = 3592  # Cerbos HTTP API port (SDK uses HTTP, not gRPC)
    tls: bool = False
    # Allow plaintext HTTP to non-loopback hosts (e.g., Docker Compose service names)
    allow_plaintext: bool = False


class SessionPoolConfig(BaseModel):
    max_per_key: int = 10
    ttl_seconds: int = 300
    health_check_interval_seconds: int = 60
    circuit_breaker_threshold: int = 5
    circuit_breaker_reset_seconds: int = 60


class ObservabilityConfig(BaseModel):
    otlp_endpoint: str = "http://localhost:4317"
    traces_sampler_arg: float = 0.1
    internal_tracing: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COMMANDCLAW_",
        env_nested_delimiter="__",
    )

    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    servers: dict[str, ServerConfig] = Field(default_factory=dict)
    # Agent access loaded separately from agents.json
    access: dict[str, AccessEntry] = Field(default_factory=dict)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    cerbos: CerbosConfig = Field(default_factory=CerbosConfig)
    session_pool: SessionPoolConfig = Field(default_factory=SessionPoolConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Environment variables override JSON config values (init kwargs).
        # This lets docker-compose env vars override ~/.commandclaw/mcp.json.
        return (env_settings, init_settings, dotenv_settings, file_secret_settings)


_CONFIG_PATH = Path.home() / ".commandclaw" / "mcp.json"
_AGENTS_PATH = Path.home() / ".commandclaw" / "agents.json"


def load_settings(
    config_path: Path | None = None,
    agents_path: Path | None = None,
) -> Settings:
    """Load settings from mcp.json (infrastructure) + agents.json (access policy).

    Config path resolution:
      mcp.json:    config_path arg > COMMANDCLAW_CONFIG env var > ~/.commandclaw/mcp.json
      agents.json: agents_path arg > COMMANDCLAW_AGENTS env var > ~/.commandclaw/agents.json

    Env vars with COMMANDCLAW_ prefix override values from the JSON files.
    """
    # Load infrastructure config
    mcp_path = config_path or Path(os.environ.get("COMMANDCLAW_CONFIG", str(_CONFIG_PATH)))
    raw: dict[str, Any] = {}
    if mcp_path.exists():
        raw = json.loads(mcp_path.read_text(encoding="utf-8"))

    # Load agent access policy from separate file
    ag_path = agents_path or Path(os.environ.get("COMMANDCLAW_AGENTS", str(_AGENTS_PATH)))
    if ag_path.exists():
        raw["access"] = json.loads(ag_path.read_text(encoding="utf-8"))
    elif "access" not in raw:
        raw["access"] = {}

    return Settings(**raw)
