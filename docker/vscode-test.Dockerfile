# Image for the VSCode extension's integration tests: a real VSCode under xvfb, Node for the
# build, Python 3.14 with calu's dependencies for `calu ide` and the scripted session.
#
# Build context: a directory with the repository's pyproject.toml and uv.lock only
# (extensions/vscode/scripts/test-in-docker.sh prepares it). The repository is mounted
# read-only at /repo when the container runs; node_modules are installed inside it.
FROM node:22-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb xauth git ca-certificates \
        libasound2 libgbm1 libgtk-3-0 libnss3 libxkbfile1 libsecret-1-0 libxss1 libxshmfence1 \
        libdrm2 libatk-bridge2.0-0 libcups2 libxcomposite1 libxdamage1 libxrandr2 libpango-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /usr/local/bin/

ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PROJECT_ENVIRONMENT=/opt/calu-venv \
    UV_CACHE_DIR=/opt/uv-cache \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1

# Python 3.14 and calu's dependencies (calu itself is installed from /repo at test time).
COPY pyproject.toml uv.lock /tmp/calu/
RUN uv python install 3.14 \
    && cd /tmp/calu && uv sync --frozen --no-install-project --no-dev \
    && mkdir -p /work/ext/.vscode-test /home/node/.npm \
    && chown -R node:node /opt/uv-python /opt/calu-venv /opt/uv-cache /work /home/node/.npm

USER node
WORKDIR /work/ext
