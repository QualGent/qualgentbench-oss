"""Which model provider a CLI harness runs against, and what each pair needs (QUA-3502).

One place decides, for a harness (`codex-cli`, `claude-code`) × provider × model:

* whether the pair is supported at all (`pair_refusal`) — an unsupported pair is
  refused with a sentence that says why, before any device or model time is spent;
* which environment variables it needs (`missing_env`), named in the refusal;
* the provider-specific environment the adapter hands the CLI (`bedrock_env`).

The supported matrix is the one measured live (codex-cli 0.156.1, claude-code
2.1.32x, 2026-11-17):

* codex-cli × openai — the default; auth is the adapter's own API-key / login logic.
* codex-cli × bedrock — `model_provider = "amazon-bedrock-runtime"` with an
  INFERENCE-PROFILE id (`us.openai.gpt-6-astra`, `us.moonshotai.kimi-k3`). A bare id
  fails, a Claude model fails (the runtime's Responses route 404s), and the
  `amazon-bedrock` provider 404s for every model.
* claude-code × anthropic — the default; `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY`.
* claude-code × fireworks — `accounts/fireworks/models/*` slugs through Fireworks'
  Anthropic-compatible endpoint (Kimi K3, GLM-5.3-Flash, …). GLM is Fireworks-only.
* claude-code × bedrock — `CLAUDE_CODE_USE_BEDROCK=1`, Claude models only
  (`us.anthropic.claude-sonnet-5-5`, …); Kimi fails (unrecognized model).

Bedrock authenticates with a Bedrock API key (`AWS_BEARER_TOKEN_BEDROCK`) and a region
(`AWS_REGION`); those two are the only AWS variables an agent ever receives
(`strip_cloud_env` removes every other one from what it inherits).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from . import pricing

OPENAI, ANTHROPIC, FIREWORKS, BEDROCK = "openai", "anthropic", "fireworks", "bedrock"
PROVIDERS = (OPENAI, ANTHROPIC, FIREWORKS, BEDROCK)

#: The process-wide provider choice (`run --provider`, `provider:` in a bench config).
#: An env var, like QGB_ALLOW_CODEX_LOGIN, so every path into an episode — lanes, the
#: grader, a bare `run_episode` — reads the same one. Unset = inferred per model.
PROVIDER_ENV = "QGB_PROVIDER"

#: Each harness's own provider, used when nothing else says otherwise.
NATIVE_PROVIDER = {"codex-cli": OPENAI, "claude-code": ANTHROPIC}
#: The pairs that are wired at all; which MODELS a pair takes is `pair_refusal`'s call.
SUPPORTED = {"codex-cli": (OPENAI, BEDROCK), "claude-code": (ANTHROPIC, FIREWORKS, BEDROCK)}

#: codex-cli's built-in provider for the Bedrock runtime's OpenAI-compatible route.
CODEX_BEDROCK_PROVIDER = "amazon-bedrock-runtime"
#: Model vendors codex-cli has been verified to run on Bedrock (2026-11-17). A vendor
#: outside this list is refused rather than launched on a guess.
CODEX_BEDROCK_VENDORS = ("openai", "moonshotai")

BEDROCK_TOKEN_ENV = "AWS_BEARER_TOKEN_BEDROCK"
BEDROCK_REGION_ENV = "AWS_REGION"
FIREWORKS_KEY_ENV = ("FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY")

# Every variable through which a CLI could reach a cloud account it was not handed on
# purpose: the AWS credential/config family, and claude-code's own switches that
# reroute it to Bedrock or Vertex. Stripped from what every CLI agent inherits; a
# Bedrock episode gets its two AWS variables back from `bedrock_env`.
_CLOUD_ENV = re.compile(
    r"AWS_\w+|CLAUDE_CODE_USE_(?:BEDROCK|VERTEX)|CLAUDE_CODE_SKIP_(?:BEDROCK|VERTEX)_AUTH"
    r"|ANTHROPIC_BEDROCK_\w+|ANTHROPIC_VERTEX_\w+")


class ProviderError(ValueError):
    """An unknown provider name, or one that does not fit the harness/model."""


def normalize(provider: str | None) -> str | None:
    """A provider name in canonical form, or None for unset/empty. Unknown → raise."""
    p = (provider or "").strip().lower()
    if not p:
        return None
    if p not in PROVIDERS:
        raise ProviderError(f"unknown provider {provider!r}; one of {', '.join(PROVIDERS)}")
    return p


def resolve(agent: str, model: str | None, explicit: str | None = None) -> str | None:
    """The provider an episode of ``agent`` on ``model`` runs against.

    ``explicit`` (a caller's own choice) wins, then `QGB_PROVIDER`, then the model id's
    shape — a Fireworks slug is Fireworks, a Bedrock inference-profile id
    (`us.<vendor>.<model>`) is Bedrock — then the harness's own provider. None for a
    harness this module does not route (the native adapter)."""
    if agent not in NATIVE_PROVIDER:
        return None
    chosen = normalize(explicit) or normalize(os.environ.get(PROVIDER_ENV))
    if chosen:
        return chosen
    return pricing.provider_of_model(model) or NATIVE_PROVIDER[agent]


def pair_refusal(agent: str, provider: str | None, model: str | None) -> str | None:
    """Why ``agent`` × ``provider`` × ``model`` cannot run, or None when it can."""
    if agent not in NATIVE_PROVIDER or provider is None:
        return None
    if provider not in SUPPORTED[agent]:
        return (f"--agent {agent} has no {provider} route (supported: "
                f"{', '.join(SUPPORTED[agent])})")
    m = (model or "").strip()
    routed = pricing.provider_of_model(m)
    if provider == BEDROCK:
        parts = pricing.bedrock_parts(m)
        if parts is None:
            return (f"{agent} on Bedrock needs an inference-profile model id "
                    f"(`us.<vendor>.<model>`, e.g. `us.moonshotai.kimi-k3`), got "
                    f"{m or 'no --models'}; bare model ids fail on the Bedrock runtime")
        vendor = parts[1]
        if agent == "claude-code" and vendor != "anthropic":
            return (f"claude-code on Bedrock runs Claude models only "
                    f"(`us.anthropic.claude-…`), got {m}; run {vendor} models with "
                    f"--agent codex-cli on Bedrock, or claude-code on Fireworks")
        if agent == "codex-cli" and vendor not in CODEX_BEDROCK_VENDORS:
            hint = (" — run Claude with --agent claude-code --provider bedrock"
                    if vendor == "anthropic" else "")
            return (f"codex-cli on Bedrock is verified for "
                    f"{', '.join(CODEX_BEDROCK_VENDORS)} models only, got {m}{hint}")
        return None
    if provider == FIREWORKS:
        if routed != FIREWORKS:
            return (f"claude-code on Fireworks needs a Fireworks slug "
                    f"({pricing.FIREWORKS_PREFIX}<model>), got {m or 'no --models'}")
        return None
    # The harness's own provider: a model id shaped for another route would be sent to
    # the wrong endpoint and fail there, with the cost of staging already paid.
    if routed is not None:
        return (f"model {m} is a {routed} model id, but the provider is {provider}; "
                f"pass --provider {routed}")
    return None


def missing_env(provider: str | None, environ: Mapping[str, str] | None = None) -> list[str]:
    """The environment variables ``provider`` needs that are unset (Bedrock and
    Fireworks only; OpenAI and Anthropic auth are the adapters' own checks)."""
    env = os.environ if environ is None else environ
    if provider == BEDROCK:
        return [v for v in (BEDROCK_TOKEN_ENV, BEDROCK_REGION_ENV)
                if not (env.get(v) or "").strip()]
    if provider == FIREWORKS:
        if not any((env.get(v) or "").strip() for v in FIREWORKS_KEY_ENV):
            return [FIREWORKS_KEY_ENV[0]]
    return []


def problems(agent: str, provider: str | None, model: str | None) -> list[str]:
    """Every reason an episode of this pair cannot start: the pair, then each missing
    variable by name. Empty = good to go (as far as the provider is concerned)."""
    if (refusal := pair_refusal(agent, provider, model)) is not None:
        return [refusal]
    out = []
    for var in missing_env(provider):
        if provider == BEDROCK:
            out.append(f"{agent} on Bedrock needs {var} in the environment (.env): "
                       + ("a Bedrock API key" if var == BEDROCK_TOKEN_ENV
                          else "the region the inference profile is served from, "
                               "e.g. us-east-2"))
        else:
            out.append(f"model {model} runs on Fireworks but {var} is not set "
                       f"(or {FIREWORKS_KEY_ENV[1]})")
    return out


def bedrock_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The only AWS variables a Bedrock episode's CLI receives. Raises when either is
    missing — preflight names them first; this is the backstop for a bare run."""
    env = os.environ if environ is None else environ
    if missing := missing_env(BEDROCK, env):
        raise ProviderError(f"Bedrock needs {' and '.join(missing)} in the environment")
    return {BEDROCK_TOKEN_ENV: env[BEDROCK_TOKEN_ENV].strip(),
            BEDROCK_REGION_ENV: env[BEDROCK_REGION_ENV].strip()}


def strip_cloud_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` less every cloud-account variable (`_CLOUD_ENV`): the host's AWS
    profile, keys and session token never reach an agent, and claude-code is never
    silently rerouted to Bedrock or Vertex by a variable the host happened to set."""
    return {k: v for k, v in env.items() if not _CLOUD_ENV.fullmatch(k)}
