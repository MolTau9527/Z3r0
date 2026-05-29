---
name: crt.sh
description: Use crt.sh Certificate Transparency search for authorized domain reconnaissance, subdomain discovery, certificate issuer review, and TLS exposure inventory.
---

# crt.sh

Use `crt.sh` for passive, authorized Certificate Transparency reconnaissance against in-scope domains. It queries public CT data and does not probe the target host directly.

## Sandbox Paths

- Wrapper script: `/root/.agents/skills/crt.sh/scripts/crt.sh`
- Upstream service: `https://crt.sh/`

## Custom Script

```sh
/root/.agents/skills/crt.sh/scripts/crt.sh <domain>
```

Always call the wrapper by absolute path. It uses `https://crt.sh/?q=...&output=json`, URL-encodes the query, and normalizes results with Python so output is stable and easy to inspect.

## Common Commands

```sh
# Discover subdomains from wildcard CT search
/root/.agents/skills/crt.sh/scripts/crt.sh example.com

# Limit output for quick triage
/root/.agents/skills/crt.sh/scripts/crt.sh example.com --limit 100

# Query only the exact domain instead of %.domain
/root/.agents/skills/crt.sh/scripts/crt.sh example.com --exact

# Keep wildcard labels such as *.example.com
/root/.agents/skills/crt.sh/scripts/crt.sh example.com --include-wildcards

# Emit normalized certificate records instead of hostnames
/root/.agents/skills/crt.sh/scripts/crt.sh example.com --json
```

## Options

- `--exact`: query the exact domain instead of the default wildcard query.
- `--include-wildcards`: preserve leading `*.` labels in hostname output.
- `--json`: output normalized certificate records with names and certificate metadata.
- `--limit <n>`: cap emitted hosts or records.
- `--timeout <seconds>`: set the curl request timeout.

## Workflow

1. Confirm the requested domain is in scope.
2. Run the wrapper with the base domain first.
3. Save results to a file when output may be large.
4. Review unique hosts, certificate issuers, issue dates, and unexpected names.
5. Treat CT names as leads. Validate separately before reporting active exposure.

## Reporting

Report:

- Query domain and whether wildcard or exact mode was used.
- Number of unique names found.
- Interesting subdomains or naming patterns.
- Notable issuers, stale certificates, or unexpected third-party infrastructure.
- Output file path when results were saved.
