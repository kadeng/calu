#!/usr/bin/env bash
# Puts socat (needed by Claude Code's Linux sandbox) into /tmp/calu-spike-tools/bin without root:
# downloads the Ubuntu packages socat and libwrap0 and extracts them with dpkg -x.
set -euo pipefail
mkdir -p /tmp/calu-spike-tools/bin && cd /tmp/calu-spike-tools
apt-get download socat libwrap0
for deb in *.deb; do dpkg -x "$deb" root; done
cat > bin/socat <<'EOF'
#!/bin/sh
LD_LIBRARY_PATH=/tmp/calu-spike-tools/root/usr/lib/x86_64-linux-gnu exec /tmp/calu-spike-tools/root/usr/bin/socat "$@"
EOF
chmod +x bin/socat
bin/socat -V | head -2
