#!/bin/sh
set -eu

usage() {
  cat <<'EOF'
Usage: crt.sh <domain> [options]

Passive Certificate Transparency lookup using https://crt.sh/.

Options:
  --exact              Query the exact domain instead of %.domain.
  --include-wildcards  Preserve leading *. labels in hostname output.
  --json               Emit normalized certificate records instead of hostnames.
  --limit <n>          Limit emitted hosts or records.
  --timeout <seconds>  curl timeout in seconds (default: 30).
  -h, --help           Show this help.
EOF
}

domain=""
mode="hosts"
wildcard=1
include_wildcards=0
limit=0
timeout_seconds=30

while [ "$#" -gt 0 ]; do
  case "$1" in
    --exact)
      wildcard=0
      ;;
    --include-wildcards)
      include_wildcards=1
      ;;
    --json)
      mode="json"
      ;;
    --limit)
      shift
      if [ "$#" -eq 0 ]; then
        echo "missing value for --limit" >&2
        exit 2
      fi
      limit="$1"
      ;;
    --timeout)
      shift
      if [ "$#" -eq 0 ]; then
        echo "missing value for --timeout" >&2
        exit 2
      fi
      timeout_seconds="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [ -n "$domain" ]; then
        echo "unexpected extra argument: $1" >&2
        usage >&2
        exit 2
      fi
      domain="$1"
      ;;
  esac
  shift
done

if [ -z "$domain" ]; then
  usage >&2
  exit 2
fi

case "$limit" in
  ''|*[!0-9]*)
    echo "--limit must be a non-negative integer" >&2
    exit 2
    ;;
esac

case "$timeout_seconds" in
  ''|*[!0-9]*)
    echo "--timeout must be a positive integer" >&2
    exit 2
    ;;
  0)
    echo "--timeout must be a positive integer" >&2
    exit 2
    ;;
esac

domain=$(
  python3 - "$domain" <<'PY'
import sys
from urllib.parse import urlparse

value = sys.argv[1].strip().lower()
if "://" in value:
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if not host or "*" in host:
        print("", end="")
        sys.exit(2)
    value = host
else:
    value = value.split("/", 1)[0]
value = value.strip().strip(".")
if value.startswith("*."):
    value = value[2:]
print(value)
PY
) || {
  echo "invalid domain input" >&2
  exit 2
}

if [ -z "$domain" ]; then
  echo "domain is empty after normalization" >&2
  exit 2
fi

python3 - "$domain" <<'PY'
import re
import sys

domain = sys.argv[1]
label_re = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

if len(domain) > 253:
    print("domain is longer than 253 characters", file=sys.stderr)
    sys.exit(2)
if "*" in domain or "_" in domain or any(ch.isspace() for ch in domain):
    print("domain contains unsupported characters", file=sys.stderr)
    sys.exit(2)
labels = domain.split(".")
if len(labels) < 2 or any(not label for label in labels):
    print("domain must contain at least two non-empty labels", file=sys.stderr)
    sys.exit(2)
for label in labels:
    try:
        ascii_label = label.encode("idna").decode("ascii")
    except UnicodeError:
        print(f"invalid IDN label: {label}", file=sys.stderr)
        sys.exit(2)
    if not label_re.fullmatch(ascii_label):
        print(f"invalid domain label: {label}", file=sys.stderr)
        sys.exit(2)
PY

query="$domain"
if [ "$wildcard" -eq 1 ]; then
  query="%.$domain"
fi

tmp_file=$(mktemp)
trap 'rm -f "$tmp_file"' EXIT INT TERM

curl -fsS --compressed --max-time "$timeout_seconds" --retry 2 --get \
  --data-urlencode "q=$query" \
  --data-urlencode "output=json" \
  "https://crt.sh/" \
  -o "$tmp_file"

python3 - "$tmp_file" "$domain" "$mode" "$include_wildcards" "$limit" <<'PY'
import json
import re
import sys

path, domain, mode, include_wildcards_raw, limit_raw = sys.argv[1:]
include_wildcards = include_wildcards_raw == "1"
limit = int(limit_raw)
host_re = re.compile(r"^\*?\.?[a-z0-9][a-z0-9_.-]*[a-z0-9]$")

try:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        raw = handle.read().strip()
    records = json.loads(raw) if raw else []
except json.JSONDecodeError as exc:
    print(f"failed to parse crt.sh JSON: {exc}", file=sys.stderr)
    sys.exit(1)

if not isinstance(records, list):
    print("unexpected crt.sh response shape", file=sys.stderr)
    sys.exit(1)

def candidate_names(record):
    for key in ("name_value", "common_name"):
        value = record.get(key)
        if not value:
            continue
        for item in str(value).splitlines():
            name = item.strip().lower().strip(".")
            if name:
                yield name

def normalize_name(name):
    if "@" in name:
        return None
    if not host_re.match(name):
        return None
    comparable = name[2:] if name.startswith("*.") else name
    if comparable != domain and not comparable.endswith("." + domain):
        return None
    if name.startswith("*.") and not include_wildcards:
        name = name[2:]
    return name

if mode == "hosts":
    hosts = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        for name in candidate_names(record):
            normalized = normalize_name(name)
            if normalized:
                hosts.add(normalized)
    output = sorted(hosts)
    if limit:
        output = output[:limit]
    for host in output:
        print(host)
elif mode == "json":
    output = []
    seen_ids = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        names = sorted(
            {
                normalized
                for name in candidate_names(record)
                for normalized in [normalize_name(name)]
                if normalized
            }
        )
        if not names:
            continue
        cert_id = record.get("id") or record.get("min_cert_id")
        dedupe_key = cert_id if cert_id is not None else tuple(names)
        if dedupe_key in seen_ids:
            continue
        seen_ids.add(dedupe_key)
        output.append(
            {
                "id": cert_id,
                "logged_at": record.get("entry_timestamp"),
                "not_before": record.get("not_before"),
                "not_after": record.get("not_after"),
                "issuer_name": record.get("issuer_name"),
                "common_name": record.get("common_name"),
                "serial_number": record.get("serial_number"),
                "names": names,
            }
        )
    output.sort(key=lambda item: (item.get("not_after") or "", str(item.get("id") or "")))
    if limit:
        output = output[:limit]
    print(json.dumps(output, indent=2, sort_keys=True))
else:
    print(f"unknown mode: {mode}", file=sys.stderr)
    sys.exit(2)
PY
