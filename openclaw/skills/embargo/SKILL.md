---
name: embargo
description: Check whether a drawing or document may be sent to someone outside the company (ITAR / EAR export control), explain a held or blocked request, and report the night sweep. Use for any question about sending files, export control, ITAR, EAR, held requests, or what the night sweep found.
---

# Embargo

Embargo is a service on this machine. It decides; you explain. Never decide a release
yourself, never approve anything, and never call any address except the one below.

Base URL: `http://host.openshell.internal:8790` (from inside the sandbox; `http://127.0.0.1:8790` on the host).
[verify on the box: OpenShell reaches host services through host.openshell.internal; the
Embargo server must then listen on 0.0.0.0, see scripts/run_gb10.sh]

## Tools (curl)

- Recent requests: `curl -s $BASE/api/requests?limit=10`
- One request with its evidence ledger and audit trail: `curl -s $BASE/api/requests/EG-XXXXXX`
- Plain-language reason: `curl -s "$BASE/api/requests/EG-XXXXXX/explain?use_model=false"`
- Check a demo file: `curl -s -X POST $BASE/api/check_path -H 'content-type: application/json' -d '{"path":"attacks/A01_bracket_views_only.png","requester":"marco.bellini@kestrelprecision.example","recipient":"hans.weber@mueller-praezision.example"}'`
- Latest night sweep: `curl -s $BASE/api/sweep/latest?limit=10`
- Audit log check: `curl -s $BASE/api/audit/verify`

## How to answer

1. Lead with the decision (RELEASE / HOLD / BLOCK) and the one rule that caused it (`codes[0]`, `reasons[0]`).
2. Name the top 2–3 ledger rows in plain words (e.g. "it is a copy of controlled drawing KP-1023 even though the label was cut off").
3. If HOLD: say only an Empowered Official (Dana Whitfield or Lee Okafor) can approve, and the requester cannot.
4. If BLOCK: say no one can approve it without an export license on file.
5. Quote numbers exactly as the API gives them. Do not invent laws, licenses or people.
