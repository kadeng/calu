# UI-testing desktop for the calu VSCode extension and (later) the PyCharm plugin.
#
# Base: the Cua "Spaces image" for Linux (Ubuntu 24.04, XFCE on Xvfb :1,
# cua-spacesd on port 3211 with the HTML5 viewer at /viewer/). Its "full" tier
# already carries git, Node 24, python3 and uv. This file adds VSCode, PyCharm
# and tmux, and two launchers (uitest-code, uitest-pycharm).
#
# The desktop user of the base image is kept: `cua` (uid 1000, home /home/cua).
#
# Build (run.sh does this):
#   docker build -t calu-uitest:latest calu/uitest
FROM ghcr.io/trycua/linux:24.04

ARG VSCODE_VERSION=1.147.0
ARG VSCODE_DEB_URL=https://vscode.download.prss.microsoft.com/dbazure/download/stable/8a14f0cc1b08d531eb83255ea3764ae22aa932b5/code_1.147.0-1794903003_amd64.deb
ARG VSCODE_DEB_SHA256=749efcdf5008a8323d76773b836199cdecf708fdb993f0f0544d9475176837ab
ARG PYCHARM_VERSION=2026.3
ARG PYCHARM_URL=https://download.jetbrains.com/python/pycharm-2026.3.tar.gz
ARG PYCHARM_SHA256=70d448ee1aa2a60234a6135b7ef9af6403fb691c26446113d587ccee06f6b813

USER root
ENV DEBIAN_FRONTEND=noninteractive

# tmux, the OpenSSH client (the PyCharm plugin's SSH mode and remote development), and the
# X11/font libraries the JetBrains runtime loads at start.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tmux git ca-certificates curl xdotool openssh-client \
      libxtst6 libxrender1 libxi6 libxext6 libfreetype6 fontconfig fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

# VSCode from the official .deb. The debconf answer stops the package from
# adding Microsoft's apt repository (the image is rebuilt to update VSCode).
RUN curl -fsSL -o /tmp/code.deb "$VSCODE_DEB_URL" \
 && echo "$VSCODE_DEB_SHA256  /tmp/code.deb" | sha256sum -c - \
 && echo "code code/add-microsoft-repo boolean false" | debconf-set-selections \
 && apt-get update \
 && apt-get install -y --no-install-recommends /tmp/code.deb \
 && rm -rf /tmp/code.deb /var/lib/apt/lists/*

# PyCharm (unified edition) from the JetBrains tarball, unpacked to /opt/pycharm.
RUN curl -fsSL -o /tmp/pycharm.tar.gz "$PYCHARM_URL" \
 && echo "$PYCHARM_SHA256  /tmp/pycharm.tar.gz" | sha256sum -c - \
 && mkdir -p /opt/pycharm \
 && tar -xzf /tmp/pycharm.tar.gz -C /opt/pycharm --strip-components=1 \
 && rm /tmp/pycharm.tar.gz \
 && ln -s /opt/pycharm/bin/pycharm /usr/local/bin/pycharm

# uv for the desktop user. The base image's full tier ships uv already; the
# Astral installer runs only when `uv` is missing for that user.
RUN runuser -u cua -- bash -lc 'command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh'

COPY --chmod=0755 uitest-code uitest-pycharm /usr/local/bin/

RUN install -d -o cua -g cua /share /workspace \
 && printf '%s\n' "vscode=$VSCODE_VERSION" "pycharm=$PYCHARM_VERSION" >/etc/calu-uitest-versions

# The base image starts as root: its entrypoint runs supervisord, which starts
# the desktop and cua-spacesd as the user `cua`. USER and CMD stay unchanged.
