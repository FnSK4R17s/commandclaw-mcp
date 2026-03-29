"""Prometheus security-focused metrics for the MCP gateway."""

from prometheus_client import Counter, Gauge, Histogram

# Request metrics
gateway_requests_total = Counter(
    "gateway_requests_total",
    "Total gateway requests",
    ["agent_id", "tool", "status"],
)

gateway_request_duration_seconds = Histogram(
    "gateway_request_duration_seconds",
    "Request duration in seconds",
    ["agent_id", "tool"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# Token rotation metrics
token_rotation_total = Counter(
    "token_rotation_total",
    "Total token rotation attempts",
    ["status"],
)

token_rotation_failures_consecutive = Gauge(
    "token_rotation_failures_consecutive",
    "Consecutive token rotation failures — alerts on >0",
)

# Session metrics
active_sessions = Gauge(
    "active_sessions",
    "Number of active sessions",
)

active_key_age_seconds = Gauge(
    "active_key_age_seconds",
    "Age of the current active key in seconds — alerts if approaching rotation interval",
)

# RBAC metrics
rbac_decisions_total = Counter(
    "rbac_decisions_total",
    "Total RBAC policy decisions",
    ["agent_id", "tool", "decision"],
)

# Validation metrics
validation_failures_total = Counter(
    "validation_failures_total",
    "Total validation failures by reason",
    ["reason"],
)

# Circuit breaker metrics
circuit_breaker_state = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=open, 2=half_open)",
    ["upstream"],
)

# Session pool metrics
session_pool_active = Gauge(
    "session_pool_active",
    "Active pooled sessions per upstream",
    ["upstream"],
)

# Rate limiting metrics
rate_limit_rejections_total = Counter(
    "rate_limit_rejections_total",
    "Total rate limit rejections",
    ["agent_id", "tool"],
)
