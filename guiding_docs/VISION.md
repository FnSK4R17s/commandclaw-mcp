# CommandClaw MCP Gateway

## Vision

CommandClaw-MCP is a Python async gateway that sits between CommandClaw agents and external MCP tool servers. It centralizes three concerns that would otherwise scatter across agent codebases: credential management, tool access control, and session routing.

Agents never see real API keys. The gateway holds all credentials and proxies tool calls on the agent's behalf. If an agent leaks its token -- through prompt injection, context dump, or log exposure -- the blast radius is bounded to 60 minutes maximum.

No single security mechanism is sufficient alone. The gateway implements layered defenses across four domains: transport fidelity, credential isolation, session management, and tool access control. Cutting any layer weakens the others.

## Problem

Without a gateway, each agent process:
- Holds raw API keys in memory, environment variables, or config files
- Discovers all available tools regardless of authorization level
- Maintains its own upstream connections with no session isolation
- Creates unbounded blast radius when credentials leak (median time from leak to first malicious use: 5 minutes)
- Operates at machine speed -- thousands of tool calls before a human could intervene
- Cannot be rate-limited, audited, or revoked at a system level

Traditional RBAC fails for AI agents specifically because:
- **Over-permissioning**: agents lack contextual judgment and will "relentlessly try to achieve" goals using all available access, making prompt injection catastrophically dangerous
- **Role explosion**: agents need hyper-specific, short-lived access patterns that static roles cannot express -- creating roles like `file-reader-agent-role-for-project-x` leads to unmanageable proliferation
- **Machine-speed risk amplification**: what causes "limited damage before someone notices" with a human becomes catastrophic within seconds as agents bulk-edit or delete thousands of records

The emerging consensus is that agents require hybrid RBAC+ABAC models with context-aware, task-scoped permissions enforced via centralized policy decision points.

---

## MCP Protocol Mechanics

The gateway must faithfully proxy the MCP protocol. These constraints are load-bearing -- violating any of them produces protocol-level bugs that are hard to diagnose.

### Wire Format: JSON-RPC 2.0

MCP uses JSON-RPC 2.0 over UTF-8. Three message types:

- **Requests**: carry `jsonrpc`, `id`, `method`, and optional `params`. The `id` field is required for response correlation -- the gateway MUST preserve this identifier exactly when forwarding.
- **Responses**: carry `jsonrpc`, `id`, and either `result` or `error`. Must echo the original request's `id`.
- **Notifications**: carry `jsonrpc` and `method` but deliberately omit `id`. They generate no response -- the gateway must not block waiting for acknowledgment.

Batch operations (JSON arrays of requests) are supported. The gateway must handle partial failures where some requests succeed and others error.

### Transport Layers

**Streamable HTTP** (current standard, 2025-11-25): single HTTP endpoint supporting both POST and GET.
- Client-to-server: individual POSTs with `Accept: application/json, text/event-stream`
- Server responds with either `application/json` (simple request-response) or `text/event-stream` (SSE for long-running operations)
- Client MUST support both response modes
- Optional GET endpoint opens an SSE stream for server-initiated messages

**stdio**: child subprocess communication via stdin/stdout. Messages are newline-delimited (no Content-Length framing). Sub-millisecond latency, 10,000+ ops/sec, but single-client only. Many MCP servers (including npm-based ones like Notion and GitHub) are stdio-only -- the gateway MUST bridge stdio servers to HTTP.

**Legacy HTTP+SSE** (2024-11-05, deprecated): dual-endpoint architecture. Support backward compatibility by attempting Streamable HTTP first and falling back to SSE on 4xx responses.

### Session Management

Sessions begin at initialization via `Mcp-Session-Id` header:
1. Server MAY assign a session ID in `InitializeResult`
2. Session IDs MUST be globally unique, cryptographically secure (UUID, JWT, or hash), visible ASCII only (0x21-0x7E)
3. Client MUST include `Mcp-Session-Id` on all subsequent requests
4. Server SHOULD reject requests missing the header with `400 Bad Request`
5. Server MAY terminate sessions at any time (responding `404 Not Found`)
6. Client MUST include `MCP-Protocol-Version` on all requests after initialization

### Resumability

Servers MAY attach `id` fields to SSE events. These function as per-stream cursors: on disconnection, the client resumes via GET with `Last-Event-ID`, and the server replays missed messages from the disconnected stream only. Disconnection SHOULD NOT be interpreted as cancellation -- clients SHOULD send explicit `CancelledNotification`.

### Gateway-Critical Protocol Constraints

- **Message ID preservation**: every response must carry the exact `id` from its request
- **Notification passthrough**: messages without `id` require no response; the gateway must not block
- **Bidirectional routing**: servers can initiate requests to clients (e.g., `sampling/createMessage`), requiring full-duplex transport through the gateway
- **Capability enforcement**: the gateway should validate messages against negotiated capabilities before forwarding
- **Session affinity**: `Mcp-Session-Id` requires sticky sessions when load balancing (unless using token-encoded sessions)
- **SSE buffering**: reverse proxies must disable response buffering for SSE streams; heartbeats (~30s) prevent intermediate proxies from timing out
- **CORS**: Streamable HTTP requires proper CORS configuration; validate origins in production

### MCP Authorization Foundation: OAuth 2.1

The MCP spec defines authorization using OAuth 2.1. MCP servers are OAuth resource servers; clients are OAuth clients. Key mechanisms:

- **Scopes**: servers include `scope` in `WWW-Authenticate` headers to indicate required permissions
- **Step-up authorization**: when a client has a token but needs additional permissions, server returns `403 Forbidden` with `error="insufficient_scope"` and required scopes
- **Default scope names** (SEP-835, Nov 2025): standardizes baseline OAuth scope names for ecosystem-wide predictability
- **M2M client credentials** (SEP-1046): adds machine-to-machine OAuth for headless agents -- cron jobs, background agents, agent-to-agent setups
- **Enterprise IdP controls** (SEP-990/XAA): routes auth through corporate identity providers for centralized admin control

The gateway's RBAC layer sits on top of this OAuth foundation. Understanding scopes, step-up auth, and M2M flows prevents conflicting with or duplicating what the MCP spec already provides.

---

## Architecture

### 1. Phantom Token Pattern (Credential Isolation)

Agents authenticate to the gateway with opaque, short-lived tokens. Real credentials never leave the gateway process.

- Agent receives: `secrets.token_urlsafe(32)` -- 256-bit random, no embedded meaning
- Gateway holds: real API keys encrypted at rest
- Request integrity: HMAC-SHA256 signed requests using the phantom token as the signing key (mandatory, not optional)
- Rotation: hourly, with 5-minute dual-key overlap window for zero-downtime
- Revocation: delete token from store, every subsequent request fails instantly

A leaked phantom token without HMAC signing means anyone can use it for the full overlap window. HMAC ensures only the legitimate holder can produce valid requests. There is no "easy mode" that skips security.

#### Credential Storage

Encrypted using Fernet (AES-128-CBC + HMAC-SHA256) with Argon2id key derivation, following ContextForge's self-describing JSON format:

```json
{"kdf":"argon2id","t":3,"m":65536,"p":1,"salt":"<base64>","token":"gAAAAA..."}
```

Per-secret unique salt embedded in the bundle. This format is self-describing -- it supports algorithm migration (e.g., moving from Fernet to AES-256-GCM) without breaking existing encrypted credentials.

Credential data model:

```python
@dataclass
class CredentialEntry:
    real_credential: str
    upstream_url: str
    header_name: str = "Authorization"
    credential_format: str = "Bearer {}"
    expires_at: Optional[float] = None

@dataclass
class PhantomSession:
    phantom_token: str
    hmac_key: str
    agent_id: str
    credentials: Dict[str, CredentialEntry]
    created_at: float
    expires_at: float
```

Multiple injection modes: header injection (`Authorization: Bearer {real_key}`), query parameter injection, Basic Auth (base64), custom path-based injection. Credential backends: environment variables (Phase 1), HashiCorp Vault and AWS Secrets Manager (future).

Credentials MUST be decrypted only at the moment of upstream injection, not held decrypted in memory. On deallocation, credential bytearrays should be zeroed via `ctypes.memset` where possible.

#### Token Distribution

Three distribution patterns for different deployment models:

1. **Environment variable injection at session start**: gateway spawns agent with `OPENAI_API_KEY=<phantom_token>` and `OPENAI_BASE_URL=http://127.0.0.1:<port>`. Standard LLM SDKs respect base URL overrides and route through the gateway automatically.
2. **Session creation endpoint**: orchestrator calls `POST /sessions` to receive a phantom token, passes it to the agent. Sessions auto-expire (default 1 hour, max 24 hours).
3. **Polling endpoint**: for agents surviving across rotation boundaries, `GET /token` returns the current token when authenticated with the previous (still valid) token. The overlap window ensures continuity during polling intervals.

#### HMAC-Signed Request Verification

Mandatory on every request. Canonical string format:

```
METHOD\nPATH\nTIMESTAMP\nNONCE\nBODY_HASH
```

Headers: `X-Phantom-Token`, `X-Timestamp`, `X-Signature`, `X-Nonce`

- Timestamp tolerance: 300 seconds (5 minutes), matching the overlap window
- Nonce cache: `OrderedDict` with TTL eviction to prevent replay attacks
- All comparisons via `hmac.compare_digest()` (constant-time, prevents timing attacks)
- Body hash: SHA-256 of the request body, included in the canonical string

#### Rotation Interval Rationale

| Environment | Interval | Rationale |
|---|---|---|
| High-security (PCI DSS) | 30-90 days | Compliance-driven |
| Standard production APIs | 90-180 days | Operational cost vs. exposure |
| AWS STS temporary credentials | 15 min - 12 hours | Cloud-native ephemeral |
| SPIFFE SVIDs | Minutes to 1 hour | Zero-trust native |
| CommandClaw-MCP | 1 hour | Balances security (60-min max exposure) vs. operational cost |

The 5-minute overlap window balances clock-skew tolerance against exposure duration. During the overlap, both old and new keys validate -- a longer overlap provides consumer flexibility but increases attack surface if the old key was already compromised.

#### Secrets Manager Unavailability

If the secrets backend (Vault, env vars) is unreachable when rotation is due: cache the current valid credential with a one-interval grace period (extend by one rotation cycle) rather than failing open. Alert on rotation failure with exponential backoff retry (up to 3 attempts). Never fail open -- a stale key is better than no authentication.

#### Full Request Lifecycle

1. Agent sends request with phantom token + HMAC signature headers
2. Gateway validates timestamp freshness (<5 min drift)
3. Gateway checks nonce uniqueness (reject replays)
4. Gateway looks up token in current generation, falls back to previous (overlap window)
5. Gateway verifies HMAC signature using phantom token as key (constant-time comparison)
6. Gateway extracts service prefix from URL path to determine which credential entry to use
7. Gateway queries Cerbos: is this agent allowed to call this tool? (see RBAC section)
8. Gateway checks rate limits for this agent + tool combination
9. Gateway strips phantom token and HMAC headers from request
10. Gateway retrieves real credential from encrypted store; refreshes if TTL expired
11. Gateway injects real credential in configured format (header, query param, Basic Auth)
12. Gateway forwards to upstream MCP server over TLS
13. Response streams back to agent without buffering (supports SSE/chunked)
14. Audit log entry written (agent_id, tool, timestamp, allowed/denied, latency -- NO tokens, NO credentials, NO request bodies)

### 2. Tool Access Control (Per-Agent RBAC)

Dual-layer enforcement using an externalized policy engine. Neither layer is sufficient alone -- discovery filtering without call-time enforcement can be bypassed by an attacker who skips the agent and sends raw HTTP requests to the gateway.

#### Phase 1: Discovery Filtering (`tools/list`)

Gateway queries the policy engine with agent identity and returns only allowed tools. This constrains the LLM's reasoning space before any action attempts -- the agent doesn't know unauthorized tools exist, so it can't attempt to use them.

Uses Cerbos batch `checkResource` API -- purpose-built for the "filter N tools for principal P" pattern. One network call filters all tools, not N individual calls.

#### Phase 2: Call-Time Enforcement (`tools/call`)

Gateway queries the policy engine with full invocation context (resource attributes, amount, department, time-of-day) before forwarding. Catches runtime conditions that discovery-time permissions can't express.

#### Deny-By-Default

New sessions start with zero tools. Only tools explicitly allowed by policy become visible. If Cerbos is unreachable, return empty tool list / deny all calls. Never fail open.

#### Dynamic Updates

`notifications/tools/list_changed` re-filters tool lists when permissions change mid-session. Cerbos supports live policy reloading without restarts -- policies can be stored on disk, in Git, or managed via Cerbos Hub.

#### Rate Limiting

Per-agent and per-tool rate limits. Agents operate at machine speed -- without rate limits, a compromised or malfunctioning agent can execute thousands of tool calls per second. RBAC controls what an agent can do; rate limiting controls how fast. Both are required.

#### Policy Engine: Cerbos

Primary choice for these reasons (vs. OPA):

| Dimension | Cerbos | OPA |
|---|---|---|
| MCP Integration | First-class SDK + demo repo | DIY via REST or WASM |
| Policy Language | YAML + CEL (reviewable by non-engineers) | Rego (steeper learning curve) |
| Performance | Sub-ms; 17x faster than OPA internals | Median ~35us, p99 ~134us |
| Deny-by-Default | Inherent (no matching rule = deny) | Must set `default allow := false` |
| Live Reload | Built-in file/git/Hub watching | Bundle API or external sync |
| Python SDK | Official async gRPC client | Community client or raw REST |
| Batch API | `checkResource` for N-tool filtering | Manual iteration |

Fall back to OPA only if the deployment already standardizes on Rego.

Cerbos policy example with ABAC conditions:

```yaml
apiVersion: "api.cerbos.dev/v1"
resourcePolicy:
  version: "default"
  resource: "mcp::tools"
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

Rich CEL conditions support: compound logic (AND/OR/NOT), time-based access control, IP-range restrictions, JWT claim checks, set intersections for team membership.

#### Capability-Based Delegation (Future)

For agent-to-agent delegation chains, RBAC role assignments become meaningless. The MCP Delegation Gateway proposes a cryptographic capability model where "delegated permissions can only shrink -- never expand." Signed, tamper-evident receipts back every authorization decision with monotonic capability reduction. This is relevant because CommandClaw is a multi-agent platform -- when Agent A delegates to Agent B, permissions must attenuate, not propagate.

### 3. Session Management

#### Redis-Backed Session Store (Primary)

- Key format: `mcp:session:{session_id}` -> JSON blob of upstream session mappings
- TTL: match key rotation interval (1 hour default)
- Immediate per-session revocation via DELETE
- Connection pooling via `redis.asyncio.ConnectionPool`
- Redis auth required in production (`redis://:password@host:6379/0`)

#### Upstream MCP Session Pooling

Connection reuse to upstream servers delivers 10-20x latency reduction (ContextForge production data). Without pooling, every tool call creates a new upstream connection at 200-500ms per connection -- unusable for interactive agent use.

- Pool key: `(server_url, identity_hash, transport_type)` -- ensures different users never share sessions
- Max sessions per key: configurable (default 10)
- Health check: ping before reuse
- Circuit breaker: opens after 5 consecutive failures for 60 seconds, then enters half-open state (1 trial request). Prevents cascading failures when upstream servers degrade.
- TTL: 300 seconds per pooled session, with idle eviction at 600 seconds

#### Token-Encoded Sessions (Horizontal Scaling)

When horizontal scaling demands it, migrate from Redis to Envoy's token-encoding pattern. All upstream session routing state encrypted into the client-facing `Mcp-Session-Id`:

Composite session ID format:
```
{routeName}@{subject}@{backend1}:{base64(sessionID1)}:{capHex1},{backend2}:{base64(sessionID2)}:{capHex2}
```

Cryptographic implementation:
- KDF: PBKDF2 with SHA-256, 16-byte random salt, 32-byte derived key, configurable iteration count
- Cipher: AES-256-GCM, random 12-byte nonce per encryption
- Wire format: `base64(salt || nonce || ciphertext)`
- Key rotation: `FallbackEnabledSessionCrypto` -- secondary seed tried if primary decryption fails, enabling zero-downtime seed rotation
- Subject binding: authenticated user identity bound into encrypted token to prevent session hijacking
- Capability bitmask: 9-bit (3-char hex) per backend covering Tools, ToolsListChanged, Prompts, PromptsListChanged, Logging, Resources, ResourcesListChanged, ResourcesSubscribe, Completions
- MUST use `asyncio.to_thread()` for PBKDF2+AES-GCM to avoid blocking the event loop

Performance: default 100K PBKDF2 iterations = ~tens of ms; tuned ~100 iterations = ~1-2ms. Cost paid only on session creation, not per JSON-RPC message.

**Known limitations:**
- No standard: Envoy-specific, no MCP spec backing
- Replay window: AES-GCM prevents forgery but not replay; upstream MCP session validation provides replay protection
- Cold start: replica receiving unseen session can decrypt but must re-establish upstream connections (resumption via `Last-Event-ID` for SSE)
- Capability staleness: flags encoded at session creation become stale if upstream changes mid-session
- Individual session revocation requires a lightweight Redis blocklist (Redis is never fully eliminated, just reduced in role)

**Trade-offs vs. Redis:**

| Dimension | Token-Encoded | Redis Store | Sticky Sessions |
|---|---|---|---|
| Horizontal scaling | Any replica, no state sync | HA Redis required | Tied to instance |
| Operational complexity | Single shared secret | Redis deployment + monitoring | LB affinity config |
| Per-request latency | ~1-2ms tuned | ~0.5-2ms network RTT | Zero |
| Failure mode | No SPOF; secret loss = rotate | Redis down = sessions lost | Instance failure = lost |
| Token size | Grows with backend count | Fixed UUID | Fixed UUID |
| Session invalidation | Cannot revoke without blocklist | Immediate DELETE | Instance-local |

### 4. Security Hardening

These are defense-in-depth measures, not optional hardening. They are part of the core architecture.

- **DNS rebinding protection**: resolve hostnames once, check all resolved IPs against a deny CIDR list, connect using pre-resolved addresses. Local proxies are vulnerable to DNS rebinding attacks without this.
- **Loopback binding**: gateway binds to `127.0.0.1` by default. Explicit override required for `0.0.0.0`. Combined with ephemeral port assignment, requires both the random port AND the phantom token for access.
- **Memory protection**: real credentials zeroed on deallocation. Use `ctypes.memset` on bytearrays or `mmap`-backed buffers. Python lacks Rust's `Zeroizing<String>`, but explicit wipe is better than relying on garbage collection.
- **MCP spec mandates**: tokens must be audience-bound (resource indicators), token passthrough is forbidden, MCP servers must not accept tokens not issued for them.
- **Encryption seed validation**: reject the default `"CHANGE-ME-IN-PRODUCTION"` at startup. Fail fast with a clear error message.
- **Cerbos TLS**: production deployments must use gRPC with TLS to Cerbos PDP.
- **Credential log stripping**: structlog processor strips any field matching `*token*`, `*key*`, `*secret*`, `*credential*`, `*password*` before writing logs.

---

## Multi-Server Aggregation

Uses FastMCP's `mount()` with namespace prefixing as the default strategy:

```python
from fastmcp import FastMCP, create_proxy

gateway = FastMCP(name="Gateway")
gateway.mount(create_proxy("http://weather-api.internal/mcp"), namespace="weather")
gateway.mount(create_proxy("http://db-api.internal/mcp"), namespace="db")
```

This produces tools like `weather_get_forecast`, `db_query`. Each proxy component routes calls back to its original upstream using the `_backend_name` mechanism -- namespace prefixing works without breaking upstream routing because the proxy calls the upstream with the original name while the gateway exposes the prefixed name.

### stdio Transport Bridging

Many MCP servers (including npm-based ones like Notion and GitHub) are stdio-only. The config uses `"command": "npx"` entries -- these are stdio servers. The gateway MUST bridge stdio servers to HTTP for the gateway to actually proxy the servers it configures.

FastMCP's `create_proxy()` handles both transport types natively. The `mcp-proxy` PyPI package can bridge stdio servers if additional control is needed.

### Credential Injection via Client Factory

Per-upstream bearer tokens via client factory customization:

```python
from fastmcp import Client
from fastmcp.client.auth import BearerAuth

def make_authed_proxy(url: str, token: str):
    def factory():
        return Client(url, auth=BearerAuth(token=token))
    return FastMCPProxy(client_factory=factory, name=f"proxy-{url}")
```

Inbound credential extraction via FastMCP middleware:

```python
class CredentialInjectionMiddleware(Middleware):
    async def on_call_tool(self, context: MiddlewareContext, call_next):
        headers = get_http_headers() or {}
        metadata = json.loads(headers.get("x-metadata", "{}"))
        context.fastmcp_context.set_state("user_creds", metadata)
        return await call_next(context)
```

### FastMCP Middleware Hooks

The security layers attach to the request pipeline via FastMCP middleware hooks:

- `on_message`: all MCP traffic (logging, metrics)
- `on_request` / `on_notification`: request vs fire-and-forget
- `on_call_tool`, `on_read_resource`, `on_get_prompt`: operation-specific interception
- `on_list_tools`, `on_list_resources`: component listing filtration

Middleware chains execute in registration order (FIFO on request, LIFO on response). Parent middleware runs for all requests; mounted server middleware only for that server's requests.

### Tool Overload Prevention

Bundling too many servers can overwhelm LLM context with tool descriptions. The Virtual MCP pattern provides on-demand discovery (up to 8 tools per request), reducing token usage by 60-85%. Implement tool count limits per session.

### Performance Characteristics

- HTTP proxy adds 200-500ms latency per hop
- Namespace depth compounds latency (nested mounts multiply round-trips) -- keep the aggregation flat
- ProxyProvider cache TTL (default 300s) means upstream tool list changes may take 5 minutes to propagate
- Session pooling reduces per-call latency by 10-20x after initial connection

---

## Observability

### OpenTelemetry Tracing

OTLP exporter to configured endpoint. Auto-instrumentation for FastAPI, httpx, redis. Span attributes: agent_id, tool_name, server_name.

### Prometheus Metrics

Security-focused metrics, not generic HTTP counters:

- `gateway_requests_total` (labels: agent_id, tool, status)
- `gateway_request_duration_seconds` (histogram)
- `token_rotation_total` (labels: status=success|failure)
- `token_rotation_failures_consecutive` (gauge -- alerts on >0)
- `active_sessions` (gauge)
- `active_key_age_seconds` (gauge -- alerts if approaching rotation interval)
- `rbac_decisions_total` (labels: agent_id, tool, decision=allow|deny)
- `validation_failures_total` (labels: reason=expired|invalid_hmac|replay|unknown_token)
- `circuit_breaker_state` (labels: upstream, state=closed|open|half_open)
- `session_pool_active` (labels: upstream)
- `rate_limit_rejections_total` (labels: agent_id, tool)

### Structured Audit Logging

Every tool call: agent_id, tool, timestamp, allowed/denied, latency. Every rotation event: old_token_hash, new_token_hash, timestamp. Every session event: create, destroy, expire. NO sensitive data: no tokens, no credentials, no request bodies, no query parameters.

structlog processor strips fields matching `*token*`, `*key*`, `*secret*`, `*credential*`, `*password*`.

### Dual Observability (ContextForge Pattern)

Internal self-contained tracing (Gantt charts, flame graphs) for development -- zero overhead when disabled. External OTLP export for production integration with Jaeger/Zipkin/Tempo/DataDog.

### Health Checks

Required for Docker Compose / Kubernetes deployment:

- `GET /health` -- liveness probe (gateway process is running)
- `GET /ready` -- readiness probe (Redis connected, Cerbos reachable, at least one upstream MCP server healthy, rotation manager active)

Without health checks, Docker cannot detect an unhealthy gateway and restart it. A stuck gateway silently fails to rotate keys, expanding the exposure window indefinitely.

---

## Emerging Standards

- **CAEP** (Continuous Access Evaluation Profile): enables longer token lifespans through real-time risk assessment and immediate revocation upon security events. Shifting toward dynamic rather than static expiration models.
- **WIMSE** (Workload Identity in Multi-System Environments): standardizes short-lived token management for cloud-native architectures.

The industry is moving toward context-aware credential lifecycle management rather than fixed-interval rotation. The gateway's architecture should accommodate this direction.

---

## Tech Stack

- **Python 3.12+** with full async/await
- **FastAPI** + **Uvicorn** -- async HTTP server
- **FastMCP** -- proxy primitives (`ProxyProvider`, `create_proxy()`, `mount()`), middleware hooks, namespace transforms
- **Cerbos** -- policy decision point for tool RBAC (async gRPC SDK, batch `checkResource`)
- **Redis** -- session store, nonce cache, rate limiting, revocation blocklist
- **cryptography** -- PBKDF2, AES-256-GCM for token-encoded sessions
- **argon2-cffi** -- Argon2id KDF for credential encryption
- **OpenTelemetry** -- OTLP traces, auto-instrumentation for FastAPI/httpx/redis
- **prometheus-client** -- security-focused metrics
- **structlog** -- structured JSON logging with credential stripping
- **httpx** -- async HTTP client for upstream communication
- **Pydantic** + **pydantic-settings** -- config validation and data models

## Configuration

Configuration is split across two files, both at `~/.commandclaw/` -- outside the vault, out of Git. The split separates infrastructure (changes rarely, contains credentials) from agent access policy (changes frequently, no secrets).

### Infrastructure: `~/.commandclaw/mcp.json`

Gateway settings, upstream server definitions (with credentials), and service connections:

```json
{
  "gateway": {
    "host": "127.0.0.1",
    "port": 8420,
    "key_rotation_interval_seconds": 3600,
    "overlap_window_seconds": 300,
    "encryption_seed": "CHANGE-ME-IN-PRODUCTION",
    "require_tls_upstream": true
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
  "redis": {
    "url": "redis://localhost:6379/0"
  },
  "cerbos": {
    "host": "localhost",
    "port": 3593,
    "tls": false
  },
  "session_pool": {
    "max_per_key": 10,
    "ttl_seconds": 300,
    "health_check_interval_seconds": 60,
    "circuit_breaker_threshold": 5,
    "circuit_breaker_reset_seconds": 60
  },
  "observability": {
    "otlp_endpoint": "http://localhost:4317",
    "traces_sampler_arg": 0.1,
    "internal_tracing": false
  }
}
```

### Agent Access Policy: `~/.commandclaw/agents.json`

Per-agent roles, tool grants, and rate limits. This file changes whenever agents are added, permissions are tuned, or rate limits are adjusted -- without touching infrastructure config or credentials:

```json
{
  "coding-agent": {
    "roles": ["developer"],
    "tools": ["github", "notion"],
    "rate_limit": { "requests_per_minute": 60 }
  },
  "research-agent": {
    "roles": ["reader"],
    "tools": ["notion"],
    "rate_limit": { "requests_per_minute": 30 }
  }
}
```

**Why separate files:**
- `mcp.json` contains real API keys and credentials -- access should be tightly controlled
- `agents.json` contains access policy only -- safe to share with agent operators without exposing secrets
- Agent access changes frequently (new agents, permission tuning, rate limit adjustments) while infrastructure is stable
- Avoids accidental credential exposure when editing agent permissions
- Aligns with the principle that policy (who can do what) is a different concern from configuration (how things connect)

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
│   ├── __main__.py                     # Entry point: load config, create app, run uvicorn
│   ├── config.py                       # Pydantic Settings from ~/.commandclaw/mcp.json + agents.json
│   │
│   ├── gateway/
│   │   ├── __init__.py
│   │   ├── app.py                      # FastAPI app, route registration, lifecycle hooks
│   │   ├── aggregator.py              # FastMCP mount() + namespace prefixing + credential injection
│   │   └── transport.py               # Streamable HTTP + stdio bridging
│   │
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── phantom.py                 # Phantom token generation, PhantomSession, TokenStore
│   │   ├── rotation.py               # Dual-key rotation manager (asyncio.Task, asyncio.Lock)
│   │   ├── hmac_verify.py            # HMAC-SHA256 canonical signing, nonce cache, timestamp check
│   │   └── credential_store.py       # Fernet + Argon2id encrypted storage, self-describing JSON
│   │
│   ├── rbac/
│   │   ├── __init__.py
│   │   ├── policy.py                  # Cerbos async gRPC client, batch checkResource
│   │   ├── discovery_filter.py        # on_list_tools middleware -- filters by principal
│   │   ├── call_guard.py             # on_call_tool middleware -- ABAC enforcement
│   │   └── rate_limiter.py           # Per-agent, per-tool rate limiting (Redis-backed)
│   │
│   ├── session/
│   │   ├── __init__.py
│   │   ├── redis_store.py            # Redis session store with TTL and connection pooling
│   │   ├── token_encoded.py          # PBKDF2 + AES-256-GCM token sessions with fallback crypto
│   │   └── pool.py                   # Upstream MCP session pooling + circuit breaker
│   │
│   ├── observability/
│   │   ├── __init__.py
│   │   ├── tracing.py                # OpenTelemetry OTLP + internal tracing toggle
│   │   ├── metrics.py                # Prometheus security-focused metrics
│   │   └── audit.py                  # Structured audit logging (no sensitive data)
│   │
│   ├── security/
│   │   ├── __init__.py
│   │   ├── dns_rebinding.py          # Resolve once, check CIDR deny list, pre-resolved connect
│   │   ├── memory.py                 # Credential zeroing utilities (ctypes.memset)
│   │   └── validation.py             # Config validation (reject default seed, enforce TLS)
│   │
│   └── middleware/
│       ├── __init__.py
│       ├── auth_middleware.py         # Phantom token + HMAC verification (mandatory)
│       ├── rbac_middleware.py         # Wires discovery_filter + call_guard + rate_limiter
│       └── logging_middleware.py      # Structured request/response logging
│
└── tests/
    ├── test_phantom.py
    ├── test_rotation.py
    ├── test_hmac.py
    ├── test_credential_store.py
    ├── test_rbac.py
    ├── test_rate_limiter.py
    ├── test_session.py
    ├── test_token_encoded.py
    ├── test_pool.py
    ├── test_aggregator.py
    ├── test_dns_rebinding.py
    └── test_security.py
```

## Key Design Decisions

- **Phantom tokens with mandatory HMAC** -- a phantom token without HMAC is just a bearer token with a fancy name. HMAC ensures only the legitimate holder can produce valid requests. There is no opt-out.
- **Cerbos over OPA** -- first-class MCP integration, YAML policies reviewable by non-engineers, batch `checkResource` for efficient tool filtering, official async Python SDK, 17x faster than OPA internals. Switch to OPA only if already deployed org-wide.
- **Redis primary, token-encoded for scale** -- Redis supports immediate session revocation and is simpler to operate. Token-encoded sessions (Envoy pattern) are architecturally superior for horizontal scaling. Even with token-encoded sessions, Redis is never eliminated -- it serves as nonce cache, rate limiter, and revocation blocklist.
- **FastMCP for aggregation** -- `ProxyProvider`, `mount()`, and namespace transforms are battle-tested proxy primitives with session isolation via client factory pattern. No need to reimplement.
- **Fetch-Then-Release for async I/O** -- never hold a database session or connection pool slot across upstream network I/O. Eager-load, copy to locals, release, then make the network call. ContextForge data: connection hold time dropped from 100ms-4min to <50ms, max concurrent requests from ~200 to ~3,000+.
- **Dual-layer tool enforcement** -- discovery filtering alone can be bypassed by raw HTTP requests. Call-time enforcement alone wastes LLM context on tools the agent cannot use. Both are required.
- **Mandatory rate limiting** -- agents operate at machine speed. RBAC controls what; rate limiting controls how fast. Both are required from day one, not deferred.
- **stdio bridging from day one** -- the config format uses `"command": "npx"` for stdio servers. Without bridging, the gateway cannot proxy its own configured servers.
- **Session pooling from day one** -- without pooling, every tool call creates a new 200-500ms connection. This makes the gateway unusable for interactive agents. 10-20x latency reduction is not a "production hardening" concern -- it is a basic performance requirement.
- **Health checks from day one** -- Docker Compose cannot restart a stuck gateway without them. A stuck gateway silently fails to rotate keys, expanding exposure indefinitely.
- **5-minute overlap, 5-minute timestamp tolerance** -- overlap window matches HMAC timestamp tolerance. Both calibrated to handle clock skew and polling intervals without extending exposure.
- **Deny-by-default everywhere** -- if Cerbos is unreachable, return empty tool list. If token validation fails, reject. If rate limit is hit, reject. Never fail open.

## Non-Scope

- **MCP protocol reimplementation** -- delegated to the MCP SDK and FastMCP
- **Agent orchestration** -- handled by commandclaw (the main project)
- **Skill management** -- handled by commandclaw-skills
- **Policy authoring UI** -- Cerbos Hub or manual YAML editing
- **Cross-agent communication** -- agents don't talk to each other through the gateway
- **Cost optimization** -- API costs are a business expense, not an engineering constraint

## Prior Art

Architecture decisions are grounded in the MCP Gateway Architecture whitepaper (124 cited sources). Key references:

| Source | What We Took |
|--------|-------------|
| MCP Specification (2025-11-25) | Transport, sessions, authorization, protocol constraints |
| Envoy AI Gateway | Token-encoded session pattern, composite session ID, fallback crypto |
| IBM ContextForge | Fetch-Then-Release, session pooling, circuit breakers, Argon2id encryption, dual observability |
| Curity / API Stronghold | Phantom token pattern for AI agents, blast radius analysis |
| CodiLime | ScopeFilterMiddleware for dual-layer tool enforcement, scope vocabulary |
| Cerbos | MCP authorization demo, batch checkResource, YAML+CEL policies |
| FastMCP | ProxyProvider, mount, namespace transforms, middleware hooks, client factory |
| Red Hat | OAuth token exchange, Vault per-user credential lookup, signed wristband headers |
| NIST SP 800-57 | Cryptoperiod guidance, key separation principles |
| GitGuardian / API Stronghold | Five-minute leak-to-exploit data, rotation insufficiency analysis |
