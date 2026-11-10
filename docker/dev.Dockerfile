# The `dev` system of docker-compose.yml: Linux with Python 3.14, uv, Node, the Claude Code CLI,
# bubblewrap, socat and git. The repository is mounted at /work; the virtual environment lives in
# the `dev-venv` volume (/venv).
#
# Claude Code's sandbox needs bubblewrap **and** socat, and it needs the container to allow
# unprivileged user namespaces, which Docker's default seccomp profile blocks — hence
# `security_opt` on the service (PLAN.md Q1, measured in spikes/RESULTS.md).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap socat git nodejs npm \
       postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

ENV UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
