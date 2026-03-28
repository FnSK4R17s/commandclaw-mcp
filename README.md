<p align="center">
  <img src="logo.png" alt="CommandClaw MCP Gateway" width="240">
</p>

<h1 align="center">CommandClaw MCP Gateway</h1>

<p align="center">
  <strong>Secure MCP proxy for AI agents. Phantom tokens. Rotating keys. Per-agent RBAC.</strong><br>
  <sub>Agents never see real credentials. The gateway handles authentication. Keys rotate every hour.</sub>
</p>

---

> [!WARNING]
> **🚧 Beta Software** — This project is under active development. Workflows and commands may be incomplete or broken. Your feedback helps make this better!
>
> 💬 **Have feedback or found a bug?**  Reach out at [**@_Shikh4r_** on X](https://x.com/_Shikh4r_)

## Why a Separate Gateway?

In OpenClaw, agents interact with external tools via ad-hoc API calls with direct access to credentials. If an agent leaks a key -- through a prompt injection, a bad tool call, or a context dump -- that key is live until someone manually rotates it. The median time from credential leak to first malicious use is **5 minutes**.

CommandClaw-MCP solves this with a proxy architecture:

```
Agent → (phantom token) → commandclaw-mcp → (real credentials) → External MCP Servers
```

The agent never holds real API keys. Even if the phantom token leaks, it expires within the hour and only works through the gateway.

## Three Security Layers

### 1. Phantom Token Pattern (Credential Isolation)

Agents receive opaque, meaningless tokens. Real API keys live only in the gateway's encrypted vault.

| What the agent sees | What the gateway holds |
|---------------------|----------------------|
| `dG9rZW5fYWJj...` (random 256-bit) | `ghp_realGitHubToken...` (encrypted at rest) |

- **Token generation**: `secrets.token_urlsafe(32)` -- pure random, no embedded meaning
- **Request integrity**: HMAC-SHA256 signed requests using the phantom token as the signing key
- **Credential encryption**: Fernet + Argon2id at rest
- **Hourly rotation**: 5-minute dual-key overlap window for zero-downtime
- **Instant revocation**: delete token from store, every subsequent request fails

A leaked phantom token provides only: temporary access (short TTL), operational access through the proxy only, scope-restricted operations, and identity-bound sessions. A leaked real credential works anywhere, from any IP, for any operation, forever.

### 2. Per-Agent Tool RBAC (Dual-Layer Enforcement)

Two authorization checks on every request. Neither is sufficient alone.

**Phase 1 -- Discovery filtering** (`tools/list`): Gateway queries the policy engine with agent identity, returns only allowed tools. The agent doesn't even know unauthorized tools exist. This constrains the LLM's reasoning space before any action attempts.

**Phase 2 -- Call-time enforcement** (`tools/call`): Gateway queries with full invocation context (resource attributes, amount, department, time-of-day) before forwarding. Catches runtime conditions that discovery-time permissions can't express.

**Deny-by-default**: New sessions start with zero tools. Only explicitly allowed tools become visible.

Policy engine: [Cerbos](https://cerbos.dev) (YAML + CEL, sub-ms decisions, async Python SDK):

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

### 3. Stateless Session Management

Upstream MCP session routing state encrypted into the client-facing `Mcp-Session-Id` using PBKDF2 + AES-256-GCM. Any gateway replica decrypts and routes without database lookups. Horizontal scaling without shared state.

Phase 1 uses Redis-backed sessions (simpler, immediate revocation). Phase 2 migrates to token-encoded sessions when scaling demands it.

## Request Lifecycle

```
1.  Agent sends request with phantom token + HMAC signature
2.  Gateway validates timestamp freshness (<5 min drift)
3.  Gateway checks nonce uniqueness (reject replays)
4.  Gateway looks up token: current generation, then previous (overlap window)
5.  Gateway verifies HMAC signature (constant-time comparison)
6.  Gateway queries Cerbos: is this agent allowed to call this tool?
7.  Gateway strips phantom token, injects real credential
8.  Gateway forwards to upstream MCP server over TLS
9.  Response streams back to agent (supports SSE/chunked)
10. Audit log entry written (no sensitive data logged)
```

## Configuration

Gateway config at `~/.commandclaw/mcp.json` -- outside the vault, never in Git:

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
  }
}
```

Agents only need:
```
COMMANDCLAW_MCP_GATEWAY=http://localhost:8420
COMMANDCLAW_MCP_KEY=<auto-rotated>
```

## Tech Stack

- **Python 3.12+** -- full async/await
- **FastAPI + Uvicorn** -- async HTTP server
- **FastMCP** -- proxy primitives, multi-server aggregation, namespace isolation
- **Cerbos** -- policy decision point for tool RBAC (async gRPC)
- **Redis** -- session store (phase 1), revocation blocklist (phase 2)
- **cryptography** -- PBKDF2, AES-256-GCM, Fernet for credential encryption
- **OpenTelemetry** -- OTLP traces, Prometheus metrics

## Quick Start

```bash
# Clone the repo
gh repo clone FnSK4R17s/commandclaw-mcp
cd commandclaw-mcp

# Configure
cp .env.example .env
# Edit ~/.commandclaw/mcp.json with your MCP server configs

# Run with Docker Compose (gateway + Redis + Cerbos)
docker compose up
```

## Documentation

See [guiding_docs/VISION.md](guiding_docs/VISION.md) for the full architecture, project structure, design decisions, and implementation scope.

Architecture decisions are grounded in a [124-source research whitepaper](https://github.com/FnSK4R17s/commandclaw/tree/main/whitepaper-output/mcp-gateway-architecture) covering MCP protocol mechanics, the gateway landscape, phantom tokens, rotating keys, RBAC for agents, and Python implementation patterns.

## Repositories

| Repo | Purpose |
|------|---------|
| [commandclaw](https://github.com/FnSK4R17s/commandclaw) | Agent runtime, Telegram I/O, tracing |
| [commandclaw-mcp](https://github.com/FnSK4R17s/commandclaw-mcp) | MCP gateway -- credential proxy, RBAC, session management |
| [commandclaw-skills](https://github.com/FnSK4R17s/commandclaw-skills) | Skills library -- `npx skills add FnSK4R17s/commandclaw-skills` |
| [commandclaw-vault](https://github.com/FnSK4R17s/commandclaw-vault) | Vault template -- clone to create a new agent |
