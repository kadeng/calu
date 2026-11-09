# The `dev` system of docker-compose.yml: Linux with Python 3.14, uv, Node, the Claude Code CLI,
# bubblewrap and git. The repository is mounted at /work; the virtual environment lives in the
# `dev-venv` volume (/venv).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap git nodejs npm postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

ENV UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
