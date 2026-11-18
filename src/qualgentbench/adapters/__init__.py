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


def auth_refusal(name: str, provider: str | None = None) -> str | None:
    """Why agent `name` must not launch on the credentials it would use now (codex-cli
    on the operator's account login without QGB_ALLOW_CODEX_LOGIN, QUA-2868), or None.
    `provider` as in `providers.resolve` (None = QGB_PROVIDER, else the agent's own).
    No side effects; an unknown agent is not refused here (`get_adapter` says so)."""
    cls = REGISTRY.get(name)
    return ask_auth_refusal(cls(), provider) if cls is not None else None


def ask_auth_refusal(adapter: AgentAdapter, provider: str | None) -> str | None:
    """`adapter.auth_refusal`, handed ``provider`` only when the override accepts it:
    an adapter written to the zero-argument contract (before QUA-3502) still works."""
    import inspect
    try:
        params = inspect.signature(adapter.auth_refusal).parameters
    except (TypeError, ValueError):
        params = {}
    if params:
        return adapter.auth_refusal(provider)
    return adapter.auth_refusal()
