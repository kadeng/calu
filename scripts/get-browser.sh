#!/usr/bin/env bash
# Install the headless browser for the web interface tests, without root.
#
# Playwright's Chromium needs libnss3 and libnspr4, which Ubuntu does not install by default and
# which `playwright install --with-deps` would want root for. So the two packages are downloaded
# and unpacked into the cache, the same trick `spikes/get-socat.sh` uses for socat, and the tests
# put that directory on LD_LIBRARY_PATH.
#
#     scripts/get-browser.sh && uv run pytest -m browser
#
# Without this the browser tests skip; every other test is unaffected.
set -euo pipefail

LIBS="${CALU_BROWSER_LIBS:-$HOME/.cache/calu-browser-libs}"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.cache/calu-venv}"

echo "== the Playwright browser (chromium-headless-shell)"
uv run playwright install chromium --only-shell

echo "== libnss3 and libnspr4 into $LIBS (no root)"
rm -rf "$LIBS"
mkdir -p "$LIBS/debs"
(cd "$LIBS/debs" && apt-get download libnss3 libnspr4)
for deb in "$LIBS"/debs/*.deb; do dpkg-deb -x "$deb" "$LIBS/root"; done
rm -rf "$LIBS/debs"

echo "== done: $(ls "$LIBS/root/usr/lib/x86_64-linux-gnu" | wc -l) libraries in $LIBS"
