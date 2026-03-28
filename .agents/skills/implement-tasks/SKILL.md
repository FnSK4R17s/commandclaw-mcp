---
name: implement-tasks
description: Pre-flight checklist and guardrails for implementing tasks and bugfixes from brainstorming docs. Use before writing any code to ensure you've read the vision, checked module impact, and aligned with project conventions.
license: MIT
compatibility: Claude Code, Cursor, Kilo Code, any CLI-capable agent
metadata:
  author: FnSK4R17s
  version: "1.1"
---

# Implement Tasks & Bugfixes

A pre-flight checklist for agents implementing tasks from `brainstorming/<feature>/tasks.md` or bugfixes from `brainstorming/<feature>/bugfixNN.md`. Following this skill prevents broken builds, convention violations, and cross-module regressions.

## Before You Write Any Code

**Do ALL of these steps first. Do not skip any.**

### 1. Read the guiding documents

```bash
cat guiding_docs/VISION.md
```

Key things to internalize:
- **Three security layers**: phantom tokens, dual-layer RBAC, stateless sessions
- **Tech stack**: Python 3.12+, FastAPI, FastMCP, Cerbos, Redis, cryptography
- **Architecture**: gateway, auth, rbac, session, observability, middleware modules
- **Non-goals**: not reimplementing MCP protocol, not agent orchestration

### 2. Read the project structure

Understand the module layout before making changes:

```
src/commandclaw_mcp/
|- gateway/          # FastAPI app, FastMCP aggregation, transport
|- auth/             # Phantom tokens, rotation, HMAC, credential store
|- rbac/             # Cerbos client, discovery filter, call guard
|- session/          # Redis store, token-encoded sessions, pooling
|- observability/    # OpenTelemetry, Prometheus, audit logging
+- middleware/       # Auth, RBAC, logging middleware
```

### 3. Read the task

Read the full task or bugfix description. Pay attention to:
- **File(s)** listed -- these are your primary targets
- **Depends on** -- ensure prerequisite tasks are complete
- **Acceptance criteria** -- these are your definition of done
- **Code snippets** -- if the task includes proposed code, study it but don't blindly copy

### 4. Check the existing code

Before modifying any module, read the relevant files:

```bash
# Read the module you're about to change
cat src/commandclaw_mcp/<module>/<file>.py

# Check existing tests
cat tests/test_<module>.py
```

## While Implementing

### 5. Check cross-module impact

Before changing any public interface (function signatures, class APIs, Pydantic models):

```bash
# Find all importers of the module you're changing
grep -rn "from commandclaw_mcp.<module>" src/ tests/ --include="*.py"

# Find all usages of a specific class/function
grep -rn "ClassName\|function_name" src/ --include="*.py"
```

**Module dependency rules:**
- If you change `auth/` (tokens, rotation), check `middleware/auth_middleware.py` and `gateway/app.py`
- If you change `rbac/` (policy, filters), check `middleware/rbac_middleware.py`
- If you change `session/`, check `gateway/app.py` and `middleware/`
- If you change `config.py`, check **everything** -- all modules depend on it
- If you change Pydantic models, check all consumers and serializers

### 6. Follow existing patterns

Before writing new logic, find similar existing code:

```bash
grep -rn "similar_pattern" src/ --include="*.py" | head -10
```

| If you're doing... | Look at... |
|-------------------|-----------|
| Adding a FastAPI endpoint | `gateway/app.py` |
| Adding middleware | `middleware/auth_middleware.py` |
| Async credential operations | `auth/credential_store.py` |
| Cerbos policy integration | `rbac/policy.py` |
| Redis operations | `session/redis_store.py` |
| OpenTelemetry spans | `observability/tracing.py` |
| Pydantic config models | `config.py` |

### 7. Python conventions for this project

- **Type hints** on all function signatures
- **Pydantic v2** for all data models and config
- **async/await** throughout -- never block the event loop
- **`asyncio.to_thread()`** for CPU-bound crypto (PBKDF2, AES-GCM)
- **`hmac.compare_digest()`** for all token/signature comparisons (constant-time)
- **No bare `except:`** -- catch specific exceptions
- **No `print()`** -- use structured logging
- **No hardcoded secrets** -- all credentials from config or vault

### 8. Keep files under size limits

| Lines | Status | Action |
|-------|--------|--------|
| < 300 | Good | Proceed |
| 300-500 | OK | Watch for growth |
| 500-800 | Warning | Plan to split on next change |
| > 800 | Alert | Must split into submodules |

## After Implementing

### 9. Type check

```bash
# If mypy or pyright is configured
mypy src/commandclaw_mcp/<module>/
```

### 10. Lint check

```bash
ruff check src/commandclaw_mcp/<module>/
ruff format --check src/commandclaw_mcp/<module>/
```

### 11. Test

```bash
# Run module-specific tests
pytest tests/test_<module>.py -v

# Run full test suite if you changed shared code
pytest tests/ -v
```

### 12. Update the task status

Mark the task as complete in the task file:

```markdown
# Before
- [ ] Criterion 1

# After
- [x] Criterion 1
```

## Common Pitfalls

| Pitfall | Prevention |
|---------|-----------|
| Changing a public API without updating importers | Step 5: grep for all usages first |
| Blocking the event loop with sync crypto | Use `asyncio.to_thread()` for PBKDF2/AES |
| Timing-vulnerable token comparison | Always use `hmac.compare_digest()` |
| Hardcoding secrets in source | All secrets from config, env vars, or vault |
| Adding code to an already-large file | Step 8: check line count, extract if needed |
| Not reading existing patterns first | Step 6: find similar code before writing new |
| Holding DB sessions across network I/O | Fetch-Then-Release: expunge + close before upstream calls |
| Missing type hints on public functions | Convention: type hints on all signatures |
| Using `print()` instead of logging | Convention: structured logging only |

## Module Responsibility Map

```
gateway/         -> FastAPI app, route registration, FastMCP aggregation, transport bridging
auth/            -> Phantom token gen/validation, key rotation, HMAC signing, encrypted credential store
rbac/            -> Cerbos async client, tools/list filtering, tools/call enforcement
session/         -> Redis session store, token-encoded sessions (phase 2), upstream session pooling
observability/   -> OpenTelemetry OTLP, Prometheus metrics, structured audit logging
middleware/      -> FastAPI middleware: auth verification, RBAC enforcement, request logging
config.py        -> Pydantic Settings from ~/.commandclaw/mcp.json
```

## Never Do

- Do not commit or push -- only the user commits
- Do not modify `guiding_docs/` unless explicitly asked
- Do not log real credentials, tokens, or API keys
- Do not use synchronous I/O in async code paths
- Do not skip reading the VISION.md "because it's a small change"
- Do not store secrets in the vault (Git repo) -- secrets live in `~/.commandclaw/mcp.json`
