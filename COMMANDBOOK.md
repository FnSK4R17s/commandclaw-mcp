# CommandClaw MCP Gateway — Command Book

Configuration lives in three places:

| File | What | Secrets | Hot-reload |
|------|------|---------|------------|
| `~/.commandclaw/mcp.json` | Servers, credentials, networking | Yes | Restart required |
| `~/.commandclaw/agents.json` | Agent access policy (roles, tools, rate limits) | No | Restart required |
| `policies/mcp_tools.yaml` | Role-based access control (Cerbos) | No | Automatic |

The split keeps credentials separate from access policy. You can hand `agents.json` to an agent operator without exposing API keys.

---

## Adding an MCP Server

Edit `~/.commandclaw/mcp.json` under `servers`.

### HTTP server

```json
{
  "servers": {
    "weather": {
      "url": "https://weather-api.example.com/mcp"
    }
  }
}
```

### stdio server (npm-based)

```json
{
  "servers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "ghp_..." }
    }
  }
}
```

After adding, restart the gateway:

```bash
make restart
make logs-gw | grep upstream_mounted
```

---

## Adding an Agent

Edit `~/.commandclaw/agents.json` directly, or use the Makefile:

```bash
make agent AGENT=coding-agent ROLES=developer TOOLS=github,notion
make agent AGENT=research-bot ROLES=reader TOOLS=notion
```

The file is a flat map of agent ID to access policy:

```json
{
  "coding-agent": {
    "roles": ["developer"],
    "tools": ["github", "notion"],
    "rate_limit": { "requests_per_minute": 60 }
  },
  "research-bot": {
    "roles": ["reader"],
    "tools": ["notion"],
    "rate_limit": { "requests_per_minute": 30 }
  }
}
```

- **`roles`** — determines what Cerbos allows (see "Adding a Role" below)
- **`tools`** — which servers this agent can see in `tools/list` (discovery filtering)
- **`rate_limit`** — max requests per minute

Restart the gateway after editing:

```bash
make restart
```

---

## Adding a Role

Edit `policies/mcp_tools.yaml`. Cerbos hot-reloads this file — no restart needed.

### Allow everything

```yaml
- actions: ["*"]
  effect: EFFECT_ALLOW
  roles: ["developer"]
```

### Read-only access

```yaml
- actions: ["*"]
  effect: EFFECT_ALLOW
  roles: ["reader"]
  condition:
    match:
      expr: >
        request.resource.attr.read_only == true
```

### Amount limits

```yaml
- actions: ["*"]
  effect: EFFECT_ALLOW
  roles: ["manager"]
  condition:
    match:
      expr: >
        !has(request.resource.attr.amount) ||
        request.resource.attr.amount < 10000
```

### Restrict to specific tools

```yaml
- actions: ["clock_*"]
  effect: EFFECT_ALLOW
  roles: ["clock-only"]
```

### Test it

```bash
make policy-check ROLE=developer ACTION=clock_india_time
make policy-check ROLE=reader ACTION=some_tool
make policy-check ROLE=reader ACTION=some_tool ATTR=read_only=true
make policy-check ROLE=manager ACTION=approve ATTR=amount=15000
```

Deny-by-default: if no rule matches, access is denied.

---

## How the Two Layers Work Together

```
Agent sends tools/list
    |
    v
Layer 1: agents.json (discovery filtering)
    -> Agent "coding-agent" can see servers: ["github", "notion"]
    -> Only tools from those servers are returned
    |
    v
Agent sends tools/call "github_create_issue"
    |
    v
Layer 2: Cerbos policy (call-time enforcement)
    -> Agent has role "developer"
    -> Policy allows developer to do "github_create_issue"
    -> ALLOW
    |
    v
Gateway forwards to upstream server
```

**Layer 1** (`agents.json`) controls what the agent can *see*.
**Layer 2** (`policies/mcp_tools.yaml`) controls what the agent can *do*.

---

## Full Example: New Server + Role + Agent

### 1. Add server — `~/.commandclaw/mcp.json`

```json
{
  "servers": {
    "slack": {
      "command": "npx",
      "args": ["-y", "@anthropic/mcp-server-slack"],
      "env": { "SLACK_BOT_TOKEN": "xoxb-..." }
    }
  }
}
```

### 2. Add role — `policies/mcp_tools.yaml`

```yaml
- actions: ["slack_post_message"]
  effect: EFFECT_ALLOW
  roles: ["notifier"]
```

### 3. Add agent

```bash
make agent AGENT=alert-bot ROLES=notifier TOOLS=slack
```

### 4. Restart and verify

```bash
make restart
make health
make policy-check ROLE=notifier ACTION=slack_post_message
make policy-check ROLE=notifier ACTION=slack_read_channel  # DENY
```

---

## Quick Reference

| Task | Command |
|------|---------|
| First-time setup | `make setup` |
| Start everything | `make up` |
| Stop everything | `make down` |
| Check health | `make health` |
| View logs | `make logs` |
| Create a session | `make session AGENT=coding-agent` |
| Add an agent | `make agent AGENT=name ROLES=role TOOLS=server` |
| Test a policy | `make policy-check ROLE=role ACTION=tool` |
| View metrics | `make metrics` |
| Rebuild | `make build && make restart` |

---

## Config Reference

### `~/.commandclaw/mcp.json` — infrastructure (has credentials)

```json
{
  "gateway": {
    "host": "127.0.0.1",
    "port": 8420,
    "encryption_seed": "<random string>",
    "key_rotation_interval_seconds": 3600,
    "overlap_window_seconds": 300
  },
  "servers": {
    "<name>": {
      "url": "https://...",
      "command": "npx",
      "args": ["-y", "..."],
      "env": { "API_KEY": "..." }
    }
  },
  "redis": { "url": "redis://localhost:6379/0" },
  "cerbos": { "host": "localhost", "port": 3592 }
}
```

### `~/.commandclaw/agents.json` — access policy (no secrets)

```json
{
  "<agent-id>": {
    "roles": ["<role>"],
    "tools": ["<server-name>"],
    "rate_limit": { "requests_per_minute": 60 }
  }
}
```

### `policies/mcp_tools.yaml` — Cerbos RBAC

```yaml
apiVersion: "api.cerbos.dev/v1"
resourcePolicy:
  version: "default"
  resource: "mcp::tools"
  rules:
    - actions: ["*"]
      effect: EFFECT_ALLOW
      roles: ["role-name"]
      condition:
        match:
          expr: >
            <CEL expression>
```

CEL expressions can access:
- `request.resource.attr.<name>` — resource attributes
- `request.principal.id` — agent identity
- `request.principal.roles` — agent roles
- `now()` — current time

Full CEL reference: https://docs.cerbos.dev/cerbos/latest/policies/conditions
