#!/usr/bin/env bash
# Install the monthly timer as a *user* unit (no root needed).
set -euo pipefail
cd "$(dirname "$0")"

DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$DEST"
# The unit needs absolute paths, and only this script knows where the checkout
# is - a path typed into the unit is right on exactly one machine.
ROOT="$(cd .. && pwd)"
sed "s|@ROOT@|$ROOT|g" elec-bill-fetch.service > "$DEST/elec-bill-fetch.service"
cp elec-bill-fetch.timer "$DEST/"
systemctl --user daemon-reload
systemctl --user enable --now elec-bill-fetch.timer

echo
echo "Installed. Next run:"
systemctl --user list-timers elec-bill-fetch.timer --no-pager
echo
echo "Test it now:  systemctl --user start elec-bill-fetch.service"
echo "Watch logs:   journalctl --user -u elec-bill-fetch -f"
echo "Remove:       systemctl --user disable --now elec-bill-fetch.timer"
