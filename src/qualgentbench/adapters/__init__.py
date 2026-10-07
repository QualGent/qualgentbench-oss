"""Agent adapters — one module per coding agent."""

from .base import AgentAdapter, AuthRefused, RunContext
from .claude_code import ClaudeCodeAdapter
from .codex_cli import CodexCliAdapter
from .native import NativeAdapter

REGISTRY: dict[str, type[AgentAdapter]] = {
    "claude-code": ClaudeCodeAdapter,
    "codex-cli": CodexCliAdapter,
    "native": NativeAdapter,
}


def get_adapter(name: str) -> AgentAdapter:
    cls = REGISTRY.get(name)
    if cls is None:
        available = ", ".join(REGISTRY)
        raise ValueError(f"Unknown agent '{name}'. Available: {available}")
    return cls()


def auth_refusal(name: str) -> str | None:
    """Why agent `name` must not launch on the credentials it would use now (codex-cli
    on the operator's account login without QGB_ALLOW_CODEX_LOGIN, QUA-2868), or None.
    No side effects; an unknown agent is not refused here (`get_adapter` says so)."""
    cls = REGISTRY.get(name)
    return cls().auth_refusal() if cls is not None else None
