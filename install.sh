#!/usr/bin/env bash
# Install or update calu with one command line.
#
#   curl -fsSL https://raw.githubusercontent.com/kadeng/calu/main/install.sh | sudo bash
#       as root: the installation `calu ... --sudo` needs (only root can change it)
#   curl -fsSL https://raw.githubusercontent.com/kadeng/calu/main/install.sh | bash
#       for your own user, without root
#
# Options (after `bash -s --`, e.g. `... | sudo bash -s -- --with-sandbox`):
#   --ref REF        install this branch, tag or commit of calu (default: main)
#   --source URL     install from this git repository (default: the GitHub repository)
#   --with-sandbox   also install bubblewrap and socat for Claude Code's sandbox (root only)
#   --help           print this text
#
# What it does, as root:
#   uv     uses `uv` from PATH, else installs uv into /usr/local/lib/calu/bin (not on PATH)
#   tools  installs curl and git with apt, dnf or zypper if they are missing
#   calu   `uv tool install --reinstall` into /usr/local/lib/calu (Python in
#          /usr/local/lib/calu/python), the `calu` command in /usr/local/bin; every directory
#          root's, writable by root only, which `--sudo` checks before it runs calu as root
# and for a user:
#   uv     uses `uv` from PATH, else installs it into ~/.local/bin
#   calu   `uv tool install --reinstall` with uv's defaults: the command in ~/.local/bin
#
# Running it again updates calu. It starts nothing, creates no database and changes no
# setting: `calu doctor` and `calu setup` come next (it prints them). The whole script is one
# function called on the last line, so a download that breaks off runs nothing.
#
# To install exactly what you have read, download the script of one commit, read it, run it:
#   curl -fsSLo install.sh https://raw.githubusercontent.com/kadeng/calu/<commit>/install.sh
#   sudo bash install.sh --ref <commit>

set -euo pipefail

CALU_SOURCE_DEFAULT="https://github.com/kadeng/calu"
UV_VERSION="0.12.23"                 # the uv installed when none is found (a tested version)
ROOT_HOME="/usr/local/lib/calu"      # the root installation (agent/records/PLAN.md D211)
ROOT_BIN="/usr/local/bin"

say() { printf 'calu install: %s\n' "$*" >&2; }
fail() { say "error: $*"; exit 1; }

usage() {
    sed -n '2,/^$/p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//' || true
    cat >&2 <<'EOF'
Usage: install.sh [--ref REF] [--source URL] [--with-sandbox]
  curl -fsSL https://raw.githubusercontent.com/kadeng/calu/main/install.sh | sudo bash
  curl -fsSL https://raw.githubusercontent.com/kadeng/calu/main/install.sh | bash
EOF
}

# Run a package manager to install packages (root only). Arguments: the packages.
install_packages() {
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q "$@"
    elif command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install "$@"
    else
        fail "no apt-get, dnf or zypper here; install $* yourself and run this again"
    fi
}

# Download a URL to stdout with curl or wget.
fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$1"
    else
        fail "neither curl nor wget is installed"
    fi
}

main() {
    local ref="main" source="$CALU_SOURCE_DEFAULT" sandbox=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --ref) [ $# -ge 2 ] || fail "--ref needs a value"; ref="$2"; shift 2 ;;
            --source) [ $# -ge 2 ] || fail "--source needs a value"; source="$2"; shift 2 ;;
            --with-sandbox) sandbox=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) usage; fail "unknown option: $1" ;;
        esac
    done
    case "$ref" in *[!A-Za-z0-9._/-]*|-*|"") fail "--ref takes a branch, tag or commit, not '$ref'" ;; esac
    [ "$(uname -s)" = "Linux" ] || fail "calu runs on Linux (WSL2, a container or an SSH host count)"

    local root=0
    [ "$(id -u)" -eq 0 ] && root=1

    if [ "$sandbox" -eq 1 ] && [ "$root" -eq 0 ]; then
        fail "--with-sandbox installs system packages; run the script with sudo"
    fi

    # curl (for uv's installer) and git (uv clones git sources with it).
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        if [ "$root" -eq 1 ]; then
            say "installing curl"
            install_packages curl ca-certificates
        else
            fail "neither curl nor wget is installed; install one and run this again"
        fi
    fi
    if ! command -v git >/dev/null 2>&1; then
        if [ "$root" -eq 1 ]; then
            say "installing git"
            install_packages git
        else
            fail "git is missing; install it (e.g. sudo apt install git) and run this again"
        fi
    fi

    # uv: the one on PATH, else a pinned version in a place of our own.
    local uv
    if command -v uv >/dev/null 2>&1 && [ "$root" -eq 0 ]; then
        uv="$(command -v uv)"
    elif [ "$root" -eq 1 ] && [ -x "$ROOT_HOME/bin/uv" ]; then
        uv="$ROOT_HOME/bin/uv"
    else
        local into
        if [ "$root" -eq 1 ]; then
            # Root's own copy: a uv in a user's directory on root's PATH would let that user run
            # code as root at the next update.
            into="$ROOT_HOME/bin"
            install -d -m 0755 -o root -g root "$ROOT_HOME" "$into"
        else
            into="$HOME/.local/bin"
            mkdir -p "$into"
        fi
        say "installing uv $UV_VERSION into $into"
        fetch "https://astral.sh/uv/$UV_VERSION/install.sh" \
            | env UV_INSTALL_DIR="$into" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
        uv="$into/uv"
        [ -x "$uv" ] || fail "uv was not installed into $into"
    fi

    local spec="git+${source}@${ref}"
    if [ "$root" -eq 1 ]; then
        install -d -m 0755 -o root -g root "$ROOT_HOME"
        say "installing calu ($spec) for root into $ROOT_HOME, the command in $ROOT_BIN"
        # umask 022: everything written is root's and not writable by others (`--sudo` checks it).
        (
            umask 022
            env UV_TOOL_DIR="$ROOT_HOME" UV_TOOL_BIN_DIR="$ROOT_BIN" \
                UV_PYTHON_INSTALL_DIR="$ROOT_HOME/python" UV_CACHE_DIR="$ROOT_HOME/cache" \
                "$uv" tool install --reinstall --quiet "$spec"
        )
        rm -rf "$ROOT_HOME/cache"
        # uv leaves lock files that everybody may write; nobody but root may change anything here.
        find "$ROOT_HOME" -perm -o+w -not -type l -exec chmod o-w {} +
    else
        say "installing calu ($spec) for $(id -un)"
        "$uv" tool install --reinstall --quiet "$spec"
    fi

    if [ "$sandbox" -eq 1 ]; then
        say "installing bubblewrap and socat for Claude Code's sandbox"
        install_packages bubblewrap socat
        # Ubuntu 23.10 and later let only programs with an AppArmor profile create user
        # namespaces; bubblewrap needs one for the sandbox (calu doctor reports the case).
        if [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = "1" ] \
            && [ -d /etc/apparmor.d ] && [ ! -e /etc/apparmor.d/bwrap ] && command -v apparmor_parser >/dev/null 2>&1; then
            say "adding the AppArmor profile /etc/apparmor.d/bwrap (user namespaces for bubblewrap)"
            cat >/etc/apparmor.d/bwrap <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,

  include if exists <local/bwrap>
}
EOF
            apparmor_parser -r /etc/apparmor.d/bwrap
        fi
    fi

    local calu_cmd
    if [ "$root" -eq 1 ]; then calu_cmd="$ROOT_BIN/calu"; else calu_cmd="$HOME/.local/bin/calu"; fi
    [ -x "$calu_cmd" ] || fail "calu was installed, but $calu_cmd is missing"
    say "done: $("$calu_cmd" --version 2>/dev/null || echo calu)"

    if [ "$root" -eq 1 ]; then
        cat >&2 <<'EOF'

Next steps (as your own user, not as root):
  calu doctor --sudo .     check the root arrangement
  sudo calu setup          the database and root's credentials in /etc/calu
  calu serve --sudo        the server: calu as root, Claude Code as you
  calu web --sudo          the web interface (prints the address of the chat pane too)
Run this script again to update calu.
EOF
    else
        case ":$PATH:" in
            *":$HOME/.local/bin:"*) ;;
            *) say "add ~/.local/bin to your PATH (e.g. in ~/.bashrc) to run calu by name" ;;
        esac
        cat >&2 <<'EOF'

Next steps:
  calu doctor      what is in place and what is missing
  calu setup       the database and your calu user password
  calu serve       the server; then calu web, the VSCode extension or calu attach
Run this script again to update calu.
EOF
    fi
}

main "$@"
