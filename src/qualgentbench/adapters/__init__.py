"""Agent adapters — one module per coding agent."""

from .base import AgentAdapter, RunContext
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
