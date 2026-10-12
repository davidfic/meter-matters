#!/usr/bin/env bash
# What the monthly timer runs: check the mailbox for an Eversource
# "bill ready" notification and file it.
#
# Eversource has no automated feed an individual can use (Green Button Connect
# requires registering as a third-party company), so we read your own mailbox
# instead. No utility credentials are involved.
#
# Needs:
#   ELEC_IMAP_USER   your email address
#   ELEC_IMAP_PASS   an app password - or store it in the keyring instead:
#     secret-tool store --label='elec-bill' service elec-bill account <you>
#   ELEC_IMAP_HOST   optional, defaults to imap.gmail.com
#
# Notifications go down every channel notify.py has configured - desktop,
# ntfy, email - so a failure still reaches you when you're not at this machine.
#
# Exit codes follow import_bill.py: 0 imported, 1 error, 2 nothing new.
set -uo pipefail
cd "$(dirname "$0")/.."

PYTHON="./.venv/bin/python"

# Settings come from .env; anything already in the environment wins. Loaded
# before the first notify, which reads its channels from here too.
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    case "$key" in ''|\#*) continue ;; esac
    [ -z "${!key:-}" ] && export "$key=$value"
  done < .env
fi

notify() {
  local urgency="$1" title="$2" body="$3"
  # Prints the message itself, so the journal always has it.
  "$PYTHON" notify.py --urgency "$urgency" "$title" "$body" || true
}

if [ -z "${ELEC_IMAP_USER:-}" ]; then
  notify normal "Electric bill check" \
    "Mailbox not configured - download the export from eversource.com by hand."
  exit 2
fi

# The usage exports come from a rolling ~3-year window; periods that age out
# are gone for good. Nothing can automate the download (see README), so the
# timer's job is to tell you before you lose anything.
stale=$("$PYTHON" greenbutton.py --staleness 2>&1)
if [ $? -ne 0 ]; then
  notify critical "Re-export your usage data" "$stale"
else
  echo "$stale"
fi

# Usage exports: only the portal has kWh/therms, and only a browser can reach
# it. The session is kept in a browser profile - no utility password is stored -
# so the one failure mode that needs you is an expired login. Say so loudly;
# a silent failure would leave usage quietly stale, which is the whole point of
# doing this at all.
portal=$("$PYTHON" portal.py --fetch 2>&1)
case $? in
  0) echo "$portal"; notify normal "Usage data updated" "$(echo "$portal" | tail -1)" ;;
  3) notify critical "Eversource login expired" \
       "Run: cd $(pwd) && ./portal.py --login" ;;
  2) : ;;  # nothing new to download
  *) echo "$portal"
     notify normal "Usage download failed" "$(echo "$portal" | tail -1)" ;;
esac

output=$("$PYTHON" billmail.py --imap --import --fuel any 2>&1)
status=$?
echo "$output"

# Run after the import, so a bill that just arrived isn't reported missing.
# "Nothing new" is only good news while no bill is due - this is what tells
# the two apart. It sends its own notification, at most once a week.
"$PYTHON" watchdog.py --notify

case "$status" in
  # The last line is billmail's summary ("imported N new statement(s)").
  0) notify normal "Electric bill updated" "$(echo "$output" | tail -1)" ;;
  2) : ;;  # no new bill yet - stay quiet, the timer runs twice a month
  *) notify critical "Bill import failed" "$(echo "$output" | tail -1)" ;;
esac
exit "$status"
