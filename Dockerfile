# QualGentBench harness image.
#
# Inside: the harness, an adb CLIENT, the claude and codex CLIs at pinned
# versions, and every published benchmark APK (sha256-verified at build time).
# Not inside: emulators (they stay on the host — Docker Desktop has no KVM on
# macOS/Windows), the MCP server, and any key or token.
#
#   docker build -t qualgentbench:local .
#   python scripts/launch.py bench.config.yaml        # the usual way to run it
#
# By hand (macOS/Windows; on Linux use --network host and 127.0.0.1):
#   docker run --rm -it \
#     -e ANDROID_ADB_SERVER_ADDRESS=host.docker.internal \
#     -e ANDROID_ADB_SERVER_HOST=host.docker.internal \
#     --env-file .env -v "$PWD/runs:/work/runs" \
#     -v "$PWD/bench.config.yaml:/app/bench.config.yaml:ro" \
#     qualgentbench:local run --config bench.config.yaml --devices emulator-5554 \
#       --runs-dir /work/runs --yes

FROM python:3.12-slim-bookworm

ARG CLAUDE_CODE_VERSION=latest
ARG CODEX_VERSION=latest

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # APKs baked below; the same cache layout the harness uses on a host.
    QGB_CACHE_DIR=/opt/qualgentbench/cache \
    # The harness reaches the HOST's adb server; the agent is pinned back to the
    # in-container meter by the runner. Overridden per OS by the launcher.
    ANDROID_ADB_SERVER_ADDRESS=host.docker.internal \
    ANDROID_ADB_SERVER_HOST=host.docker.internal \
    ANDROID_ADB_SERVER_PORT=5037 \
    PATH="/app/.venv/bin:${PATH}"

# adb from Debian: same wire protocol (1.0.41) as any modern host adb, so the
# client never tries to restart the host's server. nodejs for the agent CLIs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git adb nodejs npm \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
                   "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force \
 && claude --version && codex --version

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

COPY scripts ./scripts
COPY docs ./docs
COPY bench.config.example.yaml ./

# Every published APK, verified. Fails the build on a checksum mismatch, so an
# image either has the whole corpus or does not exist.
RUN python scripts/bake_apks.py

# Container-side identity for result.json provenance; the launcher overrides it
# with the image digest.
ENV QGB_IMAGE_DIGEST=local-build

# Answer-key isolation, enforced by the kernel rather than policed by the
# contamination scanner: the harness runs as root, but every AGENT subprocess is
# spawned as this unprivileged user (adapters/base.py), and /app — harness code,
# specs, derived truth — is root-only. Episode state lives under /work/runs,
# OUTSIDE the repo tree, so the agent's cwd shares nothing with /app. A non-root
# agent also means claude no longer needs the IS_SANDBOX escape hatch.
RUN useradd --uid 1000 --create-home agent \
 && mkdir -p /work/runs \
 && chown -R agent:agent /work \
 && chmod 700 /app
ENV QGB_AGENT_USER=agent

VOLUME ["/work/runs"]
ENTRYPOINT ["qualgent-bench"]
CMD ["--help"]
