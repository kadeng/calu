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
# What it does, in this order (each step is one function below):
#   1. check_system     Linux only; reads the options
#   2. need_tools       as root: installs curl and git with apt, dnf or zypper if missing
#                       (as a user: stops and names them if missing)
#   3. get_uv           uses the `uv` on PATH (as a user), or downloads uv UV_VERSION from
#                       its GitHub release and checks the archive's SHA-256 against the value
#                       written below before anything of it runs
#   4. install_calu     `uv tool install --reinstall git+<source>@<ref>`: uv downloads calu's
#                       source from that repository, its Python packages from PyPI and, if
#                       needed, a Python build. As root everything goes into /usr/local/lib/calu
#                       (the command into /usr/local/bin), owned by root and writable by root
#                       only, which `calu ... --sudo` checks before it runs calu as root.
#                       As a user it goes where uv puts tools (the command into ~/.local/bin).
#   5. add_sandbox      only with --with-sandbox: bubblewrap and socat, and on Ubuntu 23.10 and
#                       later an AppArmor profile that lets bubblewrap create user namespaces
#   6. say_next         prints the next commands
#   7. offer_setup      asks whether to run `calu setup` now, with the rights this script has
#                       (run through sudo: as root, as `sudo calu setup` would), on the terminal (/dev/tty, since the script itself arrives on
#                       standard input); not asked when CALU_INSTALL_NONINTERACTIVE is set or
#                       there is no terminal. `calu setup` asks its own questions and creates
#                       or migrates the database
#
# Apart from that setup, which you start by answering yes, it starts nothing, creates no
# database, changes no calu setting and writes nothing outside the places named above. Running it again updates calu; `calu uninstall` (or `sudo calu
# uninstall`) removes calu, its logins and its local database, but none of the programs of
# steps 2, 3 and 5. The whole script is one function called on the last line, so a download
# that breaks off runs nothing.
#
# To install exactly what you have read: download the script of one commit, read it, run it.
#   curl -fsSLo install.sh https://raw.githubusercontent.com/kadeng/calu/<commit>/install.sh
#   sudo bash install.sh --ref <commit>

set -euo pipefail

CALU_SOURCE_DEFAULT="https://github.com/kadeng/calu"
ROOT_HOME="/usr/local/lib/calu"      # the root installation (agent/records/PLAN.md D211)
ROOT_BIN="/usr/local/bin"

# uv, pinned: the version and the SHA-256 of its release archives, as published with the
# release (https://github.com/astral-sh/uv/releases/tag/0.12.23, files *.tar.gz.sha256).
UV_VERSION="0.12.23"
UV_SHA256_X86_64="9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6"
UV_SHA256_AARCH64="6524bd338177ed50d035d39354e12545e993bbeba2ecbddf0480c5b3a81d313f"

say() { printf 'calu install: %s\n' "$*" >&2; }
fail() { say "error: $*"; exit 1; }
is_root() { [ "$(id -u)" -eq 0 ]; }

usage() {
    sed -n '2,/^$/p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//' || true
    cat >&2 <<'EOF'
Usage: install.sh [--ref REF] [--source URL] [--with-sandbox]
  curl -fsSL https://raw.githubusercontent.com/kadeng/calu/main/install.sh | sudo bash
  curl -fsSL https://raw.githubusercontent.com/kadeng/calu/main/install.sh | bash
EOF
}

# Install distribution packages (root only). Arguments: the packages.
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

# Step 1. Arguments: the script's arguments. Sets REF, SOURCE and SANDBOX.
check_system() {
    REF="main" SOURCE="$CALU_SOURCE_DEFAULT" SANDBOX=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --ref) [ $# -ge 2 ] || fail "--ref needs a value"; REF="$2"; shift 2 ;;
            --source) [ $# -ge 2 ] || fail "--source needs a value"; SOURCE="$2"; shift 2 ;;
            --with-sandbox) SANDBOX=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) usage; fail "unknown option: $1" ;;
        esac
    done
    case "$REF" in *[!A-Za-z0-9._/-]*|-*|"") fail "--ref takes a branch, tag or commit, not '$REF'" ;; esac
    [ "$(uname -s)" = "Linux" ] || fail "calu runs on Linux (WSL2, a container or an SSH host count)"
    if [ "$SANDBOX" -eq 1 ] && ! is_root; then
        fail "--with-sandbox installs system packages; run the script with sudo"
    fi
}

# Step 2. curl (to download uv) and git (uv clones calu's repository with it).
need_tools() {
    if ! command -v curl >/dev/null 2>&1; then
        is_root || fail "curl is missing; install it (e.g. sudo apt install curl) and run this again"
        say "installing curl"
        install_packages curl ca-certificates
    fi
    if ! command -v git >/dev/null 2>&1; then
        is_root || fail "git is missing; install it (e.g. sudo apt install git) and run this again"
        say "installing git"
        install_packages git
    fi
}

# Step 3. Sets UV to the uv program to use.
get_uv() {
    if ! is_root && command -v uv >/dev/null 2>&1; then
        UV="$(command -v uv)"
        return
    fi
    local into
    if is_root; then
        # Root's own copy: a uv in a user's directory on root's PATH would let that user run
        # code as root at the next update.
        into="$ROOT_HOME/bin"
        install -d -m 0755 -o root -g root "$ROOT_HOME" "$into"
    else
        into="$HOME/.local/bin"
        mkdir -p "$into"
    fi
    if [ -x "$into/uv" ] && [ "$("$into/uv" --version 2>/dev/null | cut -d' ' -f1,2)" = "uv $UV_VERSION" ]; then
        UV="$into/uv"
        return
    fi
    local arch expected
    case "$(uname -m)" in
        x86_64|amd64) arch="x86_64"; expected="$UV_SHA256_X86_64" ;;
        aarch64|arm64) arch="aarch64"; expected="$UV_SHA256_AARCH64" ;;
        *) fail "no uv build is pinned for $(uname -m); install uv yourself and run this again" ;;
    esac
    local name="uv-$arch-unknown-linux-gnu"
    local work
    work="$(mktemp -d)"
    say "downloading uv $UV_VERSION ($arch) and checking its SHA-256"
    curl -fsSLo "$work/uv.tar.gz" \
        "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$name.tar.gz"
    if [ "$(sha256sum "$work/uv.tar.gz" | cut -d' ' -f1)" != "$expected" ]; then
        rm -rf "$work"
        fail "the downloaded uv archive does not have the SHA-256 written in this script; nothing was installed"
    fi
    tar -xzf "$work/uv.tar.gz" -C "$work"
    install -m 0755 "$work/$name/uv" "$work/$name/uvx" "$into/"
    rm -rf "$work"
    UV="$into/uv"
}

# Step 4. Installs calu with uv, from SOURCE at REF.
install_calu() {
    local spec="git+${SOURCE}@${REF}"
    if is_root; then
        say "installing calu ($spec) for root into $ROOT_HOME, the command in $ROOT_BIN"
        # umask 022: everything written is root's and not writable by others (`--sudo` checks it).
        (
            umask 022
            env UV_TOOL_DIR="$ROOT_HOME" UV_TOOL_BIN_DIR="$ROOT_BIN" \
                UV_PYTHON_INSTALL_DIR="$ROOT_HOME/python" UV_CACHE_DIR="$ROOT_HOME/cache" \
                "$UV" tool install --reinstall --quiet "$spec"
        )
        rm -rf "$ROOT_HOME/cache"
        # uv leaves lock files that everybody may write; nobody but root may change anything here.
        find "$ROOT_HOME" -perm -o+w -not -type l -exec chmod o-w {} +
    else
        say "installing calu ($spec) for $(id -un)"
        "$UV" tool install --reinstall --quiet "$spec"
    fi
}

# Step 5. Only with --with-sandbox (root).
add_sandbox() {
    [ "$SANDBOX" -eq 1 ] || return 0
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
}

# Step 6.
say_next() {
    local calu_cmd
    if is_root; then calu_cmd="$ROOT_BIN/calu"; else calu_cmd="$HOME/.local/bin/calu"; fi
    [ -x "$calu_cmd" ] || fail "calu was installed, but $calu_cmd is missing"
    say "done: $("$calu_cmd" --version 2>/dev/null || echo calu)"
    if is_root; then
        cat >&2 <<'EOF'

Next steps (as your own user, not as root):
  sudo calu setup          the database and your calu user password (root's logins in /etc/calu)
  calu serve --sudo --web  the server and the web interface: calu as root, Claude Code as you
  calu doctor --sudo .     what is in place and what is missing
Run this script again to update calu; `sudo calu uninstall` removes it.
EOF
    else
        case ":$PATH:" in
            *":$HOME/.local/bin:"*) ;;
            *) say "add ~/.local/bin to your PATH (e.g. in ~/.bashrc) to run calu by name" ;;
        esac
        cat >&2 <<'EOF'

Next steps:
  calu setup         the database and your calu user password
  calu serve --web   the server and the web interface; or the VSCode extension, calu attach
  calu doctor        what is in place and what is missing
Run this script again to update calu; `calu uninstall` removes it.
EOF
    fi
}

# Step 7. Offers to run `calu setup` on the terminal, unless CALU_INSTALL_NONINTERACTIVE is
# set or there is no terminal.
offer_setup() {
    local calu_cmd how rights answer
    if is_root; then
        calu_cmd="$ROOT_BIN/calu" how="sudo calu setup" rights="as root, like this script"
    else
        calu_cmd="$HOME/.local/bin/calu" how="calu setup" rights="as $(id -un), like this script"
    fi
    if [ -n "${CALU_INSTALL_NONINTERACTIVE:-}" ]; then
        say "CALU_INSTALL_NONINTERACTIVE is set: not offering \`$how\`; run it yourself"
        return 0
    fi
    if ! { : </dev/tty; } 2>/dev/null; then
        say "no terminal to ask on: run \`$how\` yourself"
        return 0
    fi
    printf '\nRun `calu setup` now (%s)? It sets up or updates the database and asks for your calu user password. [Y/n] ' "$rights" >/dev/tty
    read -r answer </dev/tty || answer="n"
    case "$answer" in
        ""|[Yy]|[Yy][Ee][Ss]) ;;
        *) say "not run; \`$how\` does it later"; return 0 ;;
    esac
    # Its questions and the password prompt read the terminal, not this script's input.
    if ! "$calu_cmd" setup </dev/tty; then
        say "\`$how\` did not finish (see above); run it again when the cause is fixed"
    fi
}

main() {
    check_system "$@"
    need_tools
    get_uv
    install_calu
    add_sandbox
    say_next
    offer_setup
}

main "$@"
