#!/usr/bin/env bash
# Before anything is committed: is any of it yours?
#
#   ./deploy/privacy-check.sh                  the files git would commit
#   ./deploy/privacy-check.sh --files-from F   the files listed in F, one per line
#
# Two questions, asked of every file git is about to be given:
#
#   1. Is it one of the files that hold your bills or your secrets? .gitignore
#      should already have kept those out; this checks that it did.
#   2. Does it *mention* something of yours? Your account numbers, the street
#      you live on, your name, and the values in your secret files are read
#      from the files on this machine, and every file to be committed is
#      searched for them - a comment or a test can quote a bill without being
#      one.
#
# Nothing of yours is printed: a match is shown as where it is and what kind of
# thing it is, with the value itself masked. Exits 1 if anything was found.
# Nothing leaves this machine, and nothing is changed.
set -euo pipefail
cd "$(dirname "$0")/.."

list=""
case "${1:-}" in
  "") ;;
  --files-from) list="${2:?--files-from needs a file}" ;;
  *) sed -n '2,/^[^#]/{/^#/p;}' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

files="$(mktemp)"
trap 'rm -f "$files"' EXIT
if [ -n "$list" ]; then
  cat "$list" > "$files"
elif git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git ls-files --cached --others --exclude-standard > "$files"
else
  echo "This isn't a git repository yet, so there is nothing git would commit." >&2
  echo "Run 'git init' first (it commits nothing), then this." >&2
  exit 2
fi

# Not exec'd: the list above is removed when this shell exits.
python3 - "$files" <<'EOF'
import csv
import email
import fnmatch
import io
import re
import sys
import zipfile
from email import policy
from email.utils import getaddresses
from pathlib import Path

candidates = [line.strip() for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]
candidates = [c for c in candidates if Path(c).is_file()]

# ---- 1. files that are yours outright ---------------------------------------
PERSONAL = ["AccountHistory.csv", "usage/*", "data/*", "tests/fixtures/*", ".env", ".env.*",
            ".secret.txt", "*pswd*", "*.pem", "*.key", "deploy/cloudrun.env", ".cache/*",
            "app.db*", "users/*", "explore/*.html", "explore/*.json"]
EXCEPT = ["tests/fixtures/synthetic/*", ".env.example", "explore/template.html"]

def is_personal(path):
    if any(fnmatch.fnmatch(path, pattern) for pattern in EXCEPT):
        return False
    return any(fnmatch.fnmatch(path, pattern) for pattern in PERSONAL)

outright = sorted(c for c in candidates if is_personal(c))

# ---- 2. what is yours, read from your own files ------------------------------
found = {}          # value, lowercased -> what kind of thing it is

def note(value, kind, least=4):
    value = " ".join(str(value or "").split())
    if len(value) >= least:
        found.setdefault(value.lower(), kind)

def read(path):
    try:
        return Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""

def street(address, kind):
    """The words that make an address yours: not the number, not 'RD', not the state."""
    first = address.split(",")[0]
    for word in re.findall(r"[A-Za-z]{5,}", first):
        note(word, kind, 5)

def statement_history(text):
    for row in csv.reader(io.StringIO(text)):
        if row:
            for digits in re.findall(r"\d{8,}", row[0]):
                note(digits, "an account number", 8)

def usage_export(text):
    lines = text.splitlines()
    for line in lines[:12]:
        cells = next(csv.reader([line]), [])
        key = cells[0].strip().lower() if cells else ""
        if key == "name" and len(cells) > 1:
            for word in re.findall(r"[A-Za-z]{3,}", cells[1]):
                note(word, "your name", 3)
        elif key == "address" and len(cells) > 1:
            street(cells[1], "your street")
        elif key in ("account number", "service") and len(cells) > 1:
            note(cells[1], "an account number", 8)
    if lines and "," in lines[0] and re.search(r"\d{5}\s*$", lines[0]):
        street(lines[0], "your street")            # the usage-history layout
    for line in lines:
        head = line.split(",")[0].strip()
        if re.fullmatch(r"\d{8,}", head):
            note(head, "an account number", 8)

for path in ["AccountHistory.csv", *sorted(map(str, Path("tests/fixtures").glob("*.csv")))]:
    statement_history(read(path))
for path in sorted(Path("usage").glob("*")) if Path("usage").is_dir() else []:
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    usage_export(archive.read(name).decode("utf-8-sig", "replace"))
        except (zipfile.BadZipFile, OSError):
            pass
    elif path.suffix.lower() == ".csv":
        usage_export(read(path))
for path in sorted(Path("tests/fixtures").glob("*.eml")) if Path("tests/fixtures").is_dir() else []:
    try:
        message = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    except OSError:
        continue
    for header in ("To", "Delivered-To", "X-Forwarded-To", "X-Original-To"):
        for _, address in getaddresses(message.get_all(header) or []):
            note(address, "your email address", 6)

SECRET_KEY = re.compile(r"SECRET|TOKEN|PASS|PSWD|KEY|DATABASE_URL", re.I)
for path in [".env", "deploy/cloudrun.env", *sorted(map(str, Path(".").glob(".env.*")))]:
    if path == ".env.example":
        continue
    for line in read(path).splitlines():
        key, equals, value = line.partition("=")
        if equals and not key.lstrip().startswith("#") and SECRET_KEY.search(key):
            note(value.strip().strip("'\""), f"the value of {key.strip()}", 8)
for path in [".secret.txt", *sorted(str(p) for p in Path(".").glob("*pswd*") if p.is_file())]:
    for line in read(path).splitlines():
        note(line, f"what is in {path}", 8)

# ---- search -----------------------------------------------------------------
def masked(value):
    return f"{value[:2]}{'*' * (len(value) - 2)}"

def text_of(path):
    """What a file says - including, for a zip, what the files inside it say."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    if len(data) > 2_000_000:
        return ""
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                return "\n".join(archive.read(name).decode("utf-8", "replace")
                                 for name in archive.namelist())
        except zipfile.BadZipFile:
            return ""
    return "" if b"\0" in data[:4096] else data.decode("utf-8", "replace")


hits = []
for path in candidates:
    if path in outright:
        continue
    for number, line in enumerate(text_of(path).splitlines(), 1):
        lowered = line.lower()
        for value, kind in found.items():
            if value not in lowered:
                continue
            # A word of yours must stand alone to count: a name inside a longer
            # word (a username, say) is not the name. Numbers and secrets count
            # wherever they appear, even inside something longer.
            if value.isalpha() and not re.search(
                    rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", lowered):
                continue
            hits.append((path, number, kind, masked(value)))

print(f"{len(candidates)} files would be committed; looked for {len(found)} things of yours in them")
if outright:
    print(f"\n{len(outright)} of them are files that hold your own data - .gitignore should name them:")
    for path in outright:
        print(f"    {path}")
if hits:
    print(f"\n{len(hits)} lines mention something of yours:")
    for path, number, kind, value in hits:
        print(f"    {path}:{number}  {kind} ({value})")
    print("\nReplace each with something invented before committing. The account numbers,\n"
          "name and address in tests/fixtures/synthetic/generate.py are there to be borrowed.")
if not outright and not hits:
    print("nothing of yours is in them")
sys.exit(1 if outright or hits else 0)
EOF
