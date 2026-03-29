.DEFAULT_GOAL := help
SHELL := /bin/bash

# --- Config ---
COMPOSE     := docker compose
GATEWAY_URL := http://localhost:8420
CONFIG_PATH := $(HOME)/.commandclaw/mcp.json

# Python one-liner to generate config JSON
define GENERATE_CONFIG
import json, secrets, pathlib
d = pathlib.Path.home() / ".commandclaw"
d.mkdir(parents=True, exist_ok=True)

mcp = d / "mcp.json"
if mcp.exists():
    print(f"{mcp} already exists, skipping")
else:
    mcp.write_text(json.dumps({
        "gateway": {"host": "127.0.0.1", "port": 8420, "encryption_seed": secrets.token_urlsafe(32)},
        "servers": {},
        "redis": {"url": "redis://localhost:6379/0"},
        "cerbos": {"host": "localhost", "port": 3592},
    }, indent=2) + "\n")
    print(f"Created {mcp}")

agents = d / "agents.json"
if agents.exists():
    print(f"{agents} already exists, skipping")
else:
    agents.write_text(json.dumps({}, indent=2) + "\n")
    print(f"Created {agents}")
endef
export GENERATE_CONFIG

# --- Help ---
.PHONY: help
help: ## Show available targets
	@echo "CommandClaw MCP Gateway"
	@echo ""
	@echo "  Setup:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; /\[Setup\]/ {printf "    \033[36m%-15s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Docker:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; /\[Docker\]/ {printf "    \033[36m%-15s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Development:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; /\[Dev\]|\[Test\]|\[Lint\]|\[Format\]/ {printf "    \033[36m%-15s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Operations:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; /\[Ops\]/ {printf "    \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# --- Setup ---
.PHONY: setup
setup: ## [Setup] First-time setup: generate config and .env
	@echo "--- Setting up CommandClaw MCP Gateway ---"
	@echo ""
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		SEED=$$(python3 -c "import secrets; print(secrets.token_urlsafe(32))"); \
		sed -i "s|CHANGE-ME-TO-A-RANDOM-STRING|$$SEED|" .env; \
		echo "Created .env with generated encryption seed"; \
	else \
		echo ".env already exists, skipping"; \
	fi
	@python3 -c "$$GENERATE_CONFIG"
	@echo ""
	@echo "Next steps:"
	@echo "  1. Edit $(CONFIG_PATH) — add upstream MCP servers"
	@echo "  2. Edit $(HOME)/.commandclaw/agents.json — add agents with roles"
	@echo "  3. Run: make up     (Docker Compose)"
	@echo "     or:  make dev    (local gateway + Docker deps)"

# --- Docker ---
.PHONY: build
build: ## [Docker] Build gateway image
	$(COMPOSE) build

.PHONY: up
up: ## [Docker] Start all services (gateway + Redis + Cerbos)
	@if [ ! -f $(CONFIG_PATH) ]; then \
		echo "Error: $(CONFIG_PATH) not found. Run 'make setup' first."; \
		exit 1; \
	fi
	$(COMPOSE) up -d
	@echo ""
	@echo "Gateway: $(GATEWAY_URL)"
	@echo "Health:  $(GATEWAY_URL)/health"
	@echo "Metrics: $(GATEWAY_URL)/metrics"

.PHONY: down
down: ## [Docker] Stop all services
	$(COMPOSE) down

.PHONY: restart
restart: ## [Docker] Restart all services
	$(COMPOSE) restart

.PHONY: logs
logs: ## [Docker] Follow container logs
	$(COMPOSE) logs -f

.PHONY: logs-gw
logs-gw: ## [Docker] Follow gateway logs only
	$(COMPOSE) logs -f gateway

.PHONY: ps
ps: ## [Docker] Show running services
	$(COMPOSE) ps

.PHONY: clean
clean: ## [Docker] Remove containers, volumes, and built images
	$(COMPOSE) down -v --rmi local

# --- Development ---
.PHONY: install
install: ## [Dev] Install package in editable mode with dev deps
	pip install -e ".[dev]"

.PHONY: dev-deps
dev-deps: ## [Dev] Start only Redis + Cerbos in Docker
	$(COMPOSE) up -d redis cerbos
	@echo ""
	@echo "Redis:  localhost:6379"
	@echo "Cerbos: localhost:3593 (gRPC) / localhost:3592 (HTTP)"

.PHONY: dev
dev: dev-deps ## [Dev] Run gateway locally (starts Redis + Cerbos first)
	@echo ""
	@echo "Starting gateway on $(GATEWAY_URL) ..."
	python -m commandclaw_mcp

.PHONY: test
test: ## [Test] Run test suite
	python -m pytest tests/ -v

.PHONY: test-cov
test-cov: ## [Test] Run tests with coverage report
	python -m pytest tests/ -v --cov=commandclaw_mcp --cov-report=term-missing

.PHONY: lint
lint: ## [Lint] Run ruff check + mypy
	python -m ruff check src/ tests/
	python -m mypy src/

.PHONY: fmt
fmt: ## [Format] Format code with ruff
	python -m ruff format src/ tests/
	python -m ruff check --fix src/ tests/

# --- Operations ---
.PHONY: health
health: ## [Ops] Check gateway health and readiness
	@echo "--- Health ---"
	@curl -sf $(GATEWAY_URL)/health | python3 -m json.tool 2>/dev/null || echo "  UNREACHABLE"
	@echo ""
	@echo "--- Readiness ---"
	@curl -sf $(GATEWAY_URL)/ready | python3 -m json.tool 2>/dev/null || echo "  UNREACHABLE"

.PHONY: session
session: ## [Ops] Create a test agent session (AGENT=coding-agent)
	@curl -sf -X POST $(GATEWAY_URL)/sessions \
		-H "Content-Type: application/json" \
		-d '{"agent_id": "$(or $(AGENT),coding-agent)"}' | python3 -m json.tool

.PHONY: agent
agent: ## [Ops] Add agent to agents.json (AGENT=name ROLES=role1,role2 TOOLS=srv1,srv2)
	@if [ -z "$(AGENT)" ] || [ -z "$(ROLES)" ]; then \
		echo "Usage: make agent AGENT=my-agent ROLES=developer TOOLS=clock"; \
		exit 1; \
	fi
	@python3 -c "import json; from pathlib import Path; \
		p=Path.home()/'.commandclaw'/'agents.json'; \
		d=json.loads(p.read_text()) if p.exists() else {}; \
		roles='$(ROLES)'.split(','); tools='$(TOOLS)'.split(',') if '$(TOOLS)' else []; \
		d['$(AGENT)']={'roles':roles,'tools':tools,'rate_limit':{'requests_per_minute':60}}; \
		p.write_text(json.dumps(d,indent=2)+'\n'); \
		print(json.dumps(d['$(AGENT)'],indent=2))"

.PHONY: policy-check
policy-check: ## [Ops] Check Cerbos policy (ROLE=developer ACTION=tool_name)
	@if [ -z "$(ROLE)" ]; then \
		echo "Usage: make policy-check ROLE=developer ACTION=clock_india_time"; \
		echo "       make policy-check ROLE=reader ACTION=some_tool ATTR='read_only=true'"; \
		exit 1; \
	fi
	@python3 -c "import json,urllib.request as u; \
		attr=dict(x.split('=',1) for x in '$(ATTR)'.split(',') if '=' in x) if '$(ATTR)' else {}; \
		attr={k:(v=='true' if v in('true','false') else int(v) if v.isdigit() else v) for k,v in attr.items()}; \
		action='$(or $(ACTION),*)'; \
		body=json.dumps({'principal':{'id':'check','roles':['$(ROLE)']},'resources':[{'resource':{'kind':'mcp::tools','id':action,'attr':attr},'actions':[action]}]}).encode(); \
		req=u.Request('http://localhost:3592/api/check/resources',body,{'Content-Type':'application/json'}); \
		res=json.loads(u.urlopen(req).read()); \
		r=res['results'][0]; d=list(r['actions'].values())[0]; \
		print(f\"{d.replace('EFFECT_','')}  role={chr(34)}$(ROLE){chr(34)}  action={chr(34)}{action}{chr(34)}  attr={attr or '{}'}\")"

.PHONY: metrics
metrics: ## [Ops] Fetch Prometheus metrics
	@curl -sf $(GATEWAY_URL)/metrics
