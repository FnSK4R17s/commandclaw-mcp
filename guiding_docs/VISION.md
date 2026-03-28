# CommandClaw MCP Gateway

## Vision

CommandClaw-MCP is a Python async gateway that sits between CommandClaw agents and external MCP tool servers. It centralizes three concerns that would otherwise scatter across agent codebases: credential management, tool access control, and session routing.

Agents never see real API keys. The gateway holds all credentials and proxies tool calls on the agent's behalf. If an agent leaks its token -- through prompt injection, context dump, or log exposure -- the blast radius is bounded to 60 minutes maximum.

## Problem

Without a gateway, each agent process:
- Holds raw API keys in memory, environment variables, or config files
- Discovers all available tools regardless of authorization level
- Maintains its own upstream connections with no session isolation
- Creates unbounded blast radius when credentials leak (median time from leak to first malicious use: 5 minutes)

Traditional approaches -- static RBAC, periodic key rotation, sticky load balancing -- address these individually but fail to compose into a coherent security posture.

## Architecture

### Three Security Layers

**1. Phantom Token Pattern (Credential Isolation)**

Agents authenticate to the gateway with opaque, short-lived tokens. Real credentials never leave the gateway process.

- Agent receives: `secrets.token_urlsafe(32)` -- 256-bit random, no embedded meaning
- Gateway holds: real API keys encrypted at rest (Fernet + Argon2id)
- Request integrity: HMAC-SHA256 signed requests using the phantom token as the signing key
- Rotation: hourly, with 5-minute dual-key overlap window for zero-downtime
- Revocation: delete token from store, every subsequent request fails instantly

Request lifecycle:
1. Agent sends request with phantom token + HMAC signature
2. Gateway validates timestamp freshness (<5 min drift)
3. Gateway checks nonce uniqueness (reject replays)
4. Gateway looks up token in current generation, falls back to previous (overlap window)
5. Gateway verifies HMAC signature (constant-time comparison)
6. Gateway strips phantom token, injects real credential
7. Gateway forwards to upstream MCP server over TLS
8. Response streams back to agent (supports SSE/chunked)
9. Audit log entry written (no sensitive data logged)

**2. Tool Access Control (Per-Agent RBAC)**

Dual-layer enforcement using an externalized policy engine. Neither layer is sufficient alone.

- **Phase 1 -- Discovery filtering** (`tools/list`): Gateway queries policy engine with agent identity, returns only allowed tools. Constrains the LLM's reasoning space before any action attempts.
- **Phase 2 -- Call-time enforcement** (`tools/call`): Gateway queries policy engine with full invocation context (resource attributes, time, department) before forwarding. Catches cases where discovery-time permissions were correct but runtime conditions changed.
- **Deny-by-default**: New sessions start with no tools enabled. Only explicitly allowed tools become visible.
- **Dynamic updates**: `notifications/tools/list_changed` re-filters tool lists when permissions change mid-session.

Policy engine: Cerbos (YAML + CEL policies, first-class MCP integration, async Python gRPC SDK, sub-ms decisions, batch `checkResource` API for efficient tool filtering). Fall back to OPA if the deployment already standardizes on Rego.

Example policy:
```yaml
resourcePolicy:
  resource: "mcp::expenses"
  rules:
    - actions: ["list_expenses"]
      effect: EFFECT_ALLOW
      roles: ["admin", "manager", "user"]
    - actions: ["approve_expense"]
      effect: EFFECT_ALLOW
      roles: ["manager"]
      condition:
        match:
          expr: request.resource.attr.amount < 1000
    - actions: ["delete_expense"]
      effect: EFFECT_ALLOW
      roles: ["admin"]
```

**3. Session Management (Stateless Horizontal Scaling)**

Start with Redis-backed session store (simpler, immediate revocation). Plan migration to token-encoded sessions (Envoy pattern) when horizontal scaling demands it.

Token-encoded sessions: all upstream routing state encrypted into the client-facing `Mcp-Session-Id` using PBKDF2 + AES-256-GCM. Any gateway replica decrypts and routes without database lookups. The sole shared secret is the encryption seed.

Composite session ID format:
```
{routeName}@{subject}@{backend1}:{base64(sessionID1)}:{capHex1},{backend2}:{base64(sessionID2)}:{capHex2}
```

Encrypted wire format: `base64(salt || nonce || ciphertext)`. Key rotation via fallback seed for zero-downtime.

## Tech Stack

- **Python 3.12+** with full async/await
- **FastAPI** + **Uvicorn** -- dominant Python stack for MCP gateways (ContextForge, Agentic Community, LiteLLM all use it)
- **FastMCP** -- proxy primitives (`ProxyProvider`, `create_proxy()`, `mount()`) for multi-server aggregation with namespace isolation
- **Cerbos** -- policy decision point for tool RBAC (async gRPC SDK)
- **Redis** -- session store (phase 1), revocation blocklist (phase 2)
- **cryptography** -- PBKDF2, AES-256-GCM, Fernet for credential encryption
- **OpenTelemetry** -- OTLP traces, Prometheus metrics
- **Pydantic** -- config validation and data models

## Configuration

All configuration lives at `~/.commandclaw/mcp.json` -- outside the vault, out of Git:

```json
{
  "gateway": {
    "host": "0.0.0.0",
    "port": 8420,
    "key_rotation_interval_seconds": 3600,
    "overlap_window_seconds": 300,
    "encryption_seed": "CHANGE-ME-IN-PRODUCTION"
  },
  "servers": {
    "notion": {
      "command": "npx",
      "args": ["-y", "@notionhq/notion-mcp-server"],
      "env": { "NOTION_API_KEY": "ntn_..." }
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "ghp_..." }
    }
  },
  "access": {
    "coding-agent": {
      "roles": ["developer"],
      "tools": ["github", "notion"]
    },
    "research-agent": {
      "roles": ["reader"],
      "tools": ["notion"]
    }
  },
  "redis": {
    "url": "redis://localhost:6379/0"
  },
  "observability": {
    "otlp_endpoint": "http://localhost:4317",
    "traces_sampler_arg": 0.1
  }
}
```

## Project Structure

```
/apps/commandclaw-mcp/
├── guiding_docs/
│   └── VISION.md                       # This file
├── pyproject.toml
├── .env.example
├── Dockerfile
├── docker-compose.yml                  # Gateway + Redis + Cerbos
├── policies/                           # Cerbos policy files
│   └── mcp_tools.yaml
├── src/commandclaw_mcp/
│   ├── __init__.py
│   ├── __main__.py                     # Entry point
│   ├── config.py                       # Pydantic Settings from ~/.commandclaw/mcp.json
│   │
│   ├── gateway/
│   │   ├── __init__.py
│   │   ├── app.py                      # FastAPI app, route registration
│   │   ├── aggregator.py               # FastMCP mount() + namespace prefixing
│   │   └── transport.py                # Streamable HTTP + stdio bridging
│   │
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── phantom.py                  # Phantom token generation + validation
│   │   ├── rotation.py                 # Dual-key rotation manager (hourly + overlap)
│   │   ├── hmac_verify.py              # HMAC-SHA256 request signing + verification
│   │   └── credential_store.py         # Encrypted credential storage (Fernet + Argon2id)
│   │
│   ├── rbac/
│   │   ├── __init__.py
│   │   ├── policy.py                   # Cerbos client wrapper (async gRPC)
│   │   ├── discovery_filter.py         # Phase 1: tools/list filtering middleware
│   │   └── call_guard.py              # Phase 2: tools/call enforcement middleware
│   │
│   ├── session/
│   │   ├── __init__.py
│   │   ├── redis_store.py             # Phase 1: Redis-backed session store
│   │   ├── token_encoded.py           # Phase 2: PBKDF2 + AES-256-GCM token sessions
│   │   └── pool.py                    # Upstream MCP session pooling + circuit breaker
│   │
│   ├── observability/
│   │   ├── __init__.py
│   │   ├── tracing.py                 # OpenTelemetry OTLP setup
│   │   ├── metrics.py                 # Prometheus metrics (rotation, auth, circuit breaker)
│   │   └── audit.py                   # Structured audit logging (no sensitive data)
│   │
│   └── middleware/
│       ├── __init__.py
│       ├── auth_middleware.py          # Phantom token + HMAC verification
│       ├── rbac_middleware.py          # Wires discovery_filter + call_guard
│       └── logging_middleware.py       # Request/response logging
│
└── tests/
    ├── test_phantom.py
    ├── test_rotation.py
    ├── test_hmac.py
    ├── test_rbac.py
    ├── test_session.py
    └── test_aggregator.py
```

## Key Design Decisions

- **Phantom tokens over rotated real keys** -- rotation alone fails because median leak-to-exploit is 5 minutes. Phantom tokens ensure real credentials never reach the agent.
- **Cerbos over OPA** -- first-class MCP integration, YAML policies reviewable by non-engineers, batch `checkResource` API purpose-built for tool filtering, official async Python SDK. Switch to OPA only if already deployed org-wide.
- **Redis first, token-encoded later** -- Redis is simpler and supports immediate session revocation. Token-encoded sessions (Envoy pattern) are architecturally superior for horizontal scaling but add complexity. Migrate when needed.
- **FastMCP for aggregation** -- `ProxyProvider`, `mount()`, and namespace transforms are battle-tested proxy primitives. No need to reimplement multi-server aggregation.
- **Fetch-Then-Release for DB** -- learned from ContextForge: never hold a DB session across upstream network I/O. Eager-load, copy, release, then make the network call.
- **Dual-layer tool enforcement** -- discovery filtering alone can be bypassed by raw HTTP requests. Call-time enforcement alone wastes LLM context on tools the agent cannot use. Both are required.
- **5-minute overlap, 5-minute timestamp tolerance** -- overlap window matches the HMAC timestamp tolerance. Both are calibrated to handle clock skew and polling intervals without extending exposure.

## Scope

### Phase 1 -- Core Gateway
- FastAPI app with Streamable HTTP transport
- FastMCP multi-server aggregation with namespace prefixing
- Phantom token generation + hourly rotation with overlap
- HMAC-signed request verification
- Fernet + Argon2id credential encryption at rest
- Redis-backed session store
- Cerbos integration for dual-layer tool RBAC
- OpenTelemetry tracing + Prometheus metrics
- Docker Compose deployment (gateway + Redis + Cerbos)

### Phase 2 -- Production Hardening
- Token-encoded sessions (Envoy pattern) for stateless scaling
- Upstream MCP session pooling with circuit breakers
- stdio-to-HTTP transport bridging for local MCP servers
- Secrets manager integration (HashiCorp Vault, AWS Secrets Manager)
- CAEP integration for real-time risk-based revocation
- Health check endpoint + readiness/liveness probes
- Rate limiting per-agent and per-tool

### Non-Scope
- **MCP protocol reimplementation** -- delegated to the MCP SDK and FastMCP
- **Agent orchestration** -- handled by commandclaw (the main project)
- **Skill management** -- handled by commandclaw-skills
- **Policy authoring UI** -- Cerbos Hub or manual YAML editing

## References

Architecture decisions are grounded in the MCP Gateway Architecture whitepaper (124 cited sources) at `commandclaw/whitepaper-output/mcp-gateway-architecture/`. Key sources:

- MCP Specification (2025-11-25) -- transport, sessions, authorization
- Envoy AI Gateway -- token-encoded session pattern
- IBM ContextForge -- Python async gateway patterns, Fetch-Then-Release
- Curity / API Stronghold -- phantom token pattern for AI agents
- CodiLime -- ScopeFilterMiddleware for dual-layer tool enforcement
- Cerbos -- MCP authorization demo and policy engine
- FastMCP -- proxy primitives, middleware, namespace transforms
