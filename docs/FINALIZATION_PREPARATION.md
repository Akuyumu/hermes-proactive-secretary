# V0.4.0 finalization preparation — parent-run harness

Scope: prepare **auditable, parent-run** finalization scripts for (1) the real
native-MCP Firecrawl benchmark, (2) the guarded code-only cutover and (3) the
private code-only publication — plus the minimal verified-schema change the
benchmark needs. **The worker ran no live command.** No network, no secret,
no live config/state/job read, no scheduler change, no GitHub write, no
notification. Only the two permitted shell commands were executed.

Frozen candidate state: `version 0.4.0`; **904 tests OK**; **selftest 45/45**.
All new files live under `finalization/` (excluded from the release/export via
`.gitignore` and the builder's rules).

## Prepared files

| File | Purpose | Live writes |
|---|---|---|
| `finalization/common.py` | shared export rules, hashing, secret scan, atomic owner-only writes | none |
| `finalization/build_release.py` | clean code-only `release-0.4.0/` + sha256 manifest | workspace only |
| `finalization/native_mcp_benchmark.py` | real `ctx.call_mcp` benchmark in isolated teststate | parent, isolated teststate |
| `finalization/apply_cutover.py` | guarded code-only install with backup/rollback + job pause/resume | parent, live |
| `finalization/publish_private.py` | code-only push to the existing private repo, no force | parent, GitHub |
| `finalization/acceptance.example.json` | shape of the mandatory independent gate | none |

The prior cutover references copied into `host-references/prior_*.py` were read
and adapted; the actual host contract files (`host-references/plugins.py`,
`secret_scope.py`, `mcp_tool.py`, `firecrawl-contract.json`) were used as the
source of truth.

## Actual host facts used (not guessed)

From `host-references/firecrawl-contract.json`:

- `firecrawl_scrape.inputSchema`: `url` is a `string`/`uri`; `formats` is an
  array whose enum includes `markdown`; `maxAge` is a `number`; the schema sets
  `additionalProperties: false`. The harness therefore sends only
  `{url, formats:["markdown"]}` and adds `maxAge` only when a verified freshness
  bound is configured. No `proxy`/`profile`/`screenshot`/`json`/`crawl`/premium
  options are ever sent.
- `firecrawl_credit_usage.inputSchema`: `view` enum `current|historical`. The
  benchmark reads `view:"current"` first and records only booleans and an
  envelope hash — never `remainingCredits`/`planCredits` or account detail.

From `host-references/plugins.py` (`PluginContext.call_mcp`, line ~509):
`call_mcp(server, tool, arguments, timeout)` returns an envelope
`{"ok": bool, "result"|"error": ...}`, requires the server in
`plugins.entries.<plugin_id>.mcp_allowlist` (default-deny), clamps timeout to
1–600s and truncates >64KB. `apply_cutover.py` verifies the grant; the
benchmark refuses without it.

## Minimal verified-schema code change

To satisfy "match actual schema, enforce explicit `maxAge` freshness if needed":

- `src/proactive_pilot/routed_retrieval.py`: added an optional
  `firecrawl_mcp_max_age` policy knob (default `None` → field omitted, so the
  documented simple scrape shape is byte-unchanged). When set, `_invoke` adds
  `maxAge` to the one narrow request. Included in the config fingerprint.
- `src/proactive_pilot/policy.py` + `policy/policy.default.json` +
  `policy/policy.sample.json`: neutral `firecrawl_mcp_max_age: null`.
- `tests/test_v040_routed_retrieval.py`: two new config-neutral tests
  (no `maxAge` by default; `maxAge: 0` sent when configured/verified).

Evidence: `PYTHONPATH=src python3 -m unittest discover -s tests` → **Ran 904
tests … OK** (901 candidate baseline + 2 maxAge tests + 1 harness self-check;
no test weakened). The harness self-check compiles every script, asserts the
export exclusion rules and matches the benchmark schema checker against the
real contract; it skips cleanly in the installed/release tree where
`finalization/` is absent. `PYTHONPATH=src python3 -m proactive_pilot.selftest`
→ **45/45 passed**.

## Parent run sequence

```bash
# 0. freeze: install the parent suite + selftest logs and the independent gate
#    finalization/parent-final-suite.log          (must end "OK", >= 904 tests)
#    finalization/FINALIZATION_ACCEPTANCE.json    (see acceptance.example.json)

# 1. build the clean code-only release + manifest (workspace only)
python3 finalization/build_release.py

# 2. native MCP benchmark (isolated teststate; no paid call without assertion)
python3 finalization/native_mcp_benchmark.py --preflight-only \
    --server <mcp-server> --tool firecrawl_scrape --url <public-url>
python3 finalization/native_mcp_benchmark.py \
    --server <mcp-server> --tool firecrawl_scrape --url <public-url> \
    --confirm-no-paid-charge --owner-included-credits-evidence <evidence.json> --max-age 0
# -> finalization/native-mcp-benchmark.json
#    finalization/fixtures/firecrawl-result.sanitized.json

# 3. guarded code-only cutover (owner sign-off required)
HERMES_CUTOVER_OWNER_ACK=1 python3 finalization/apply_cutover.py --confirm
# optional bounded retrieval opt-in (schema verified by the benchmark):
HERMES_CUTOVER_OWNER_ACK=1 python3 finalization/apply_cutover.py --confirm \
    --enable-firecrawl --firecrawl-server <mcp-server> \
    --firecrawl-tool firecrawl_scrape --schema-verified
# -> finalization/cutover-applied.json

# 4. private code-only publication (no force, no visibility change)
python3 finalization/publish_private.py --confirm
# -> finalization/private-publication.json
```

## Acceptance gate (mandatory)

`finalization/FINALIZATION_ACCEPTANCE.json` must assert
`independent_review_passed`, `luna_accepted`, `real_no_send_passed`,
`benchmark_passed`, `no_notifications` all `true`, plus
`HERMES_CUTOVER_OWNER_ACK=1`, plus explicit authority/review identities
(`parent_authority`, `reviewer`, `luna_review_id`, nonempty `parent_review_ids`).
Booleans alone are never proof: `apply_cutover.py` recomputes and links the
`benchmark_report`, `no_send_report` and `suite_log` sha256 entries, requires
the benchmark report to show a native-context render with a valid grant, the
no-send report to show zero notifications, `suite_exit_code == 0`, and
`frozen_manifest_sha256` to equal the release manifest. The suite log must end
`OK` with at least the frozen 904 baseline.

## Guarantees (fail closed)

- **Benchmark**: contract + grant + `ctx.call_mcp` verified before any call;
  credit usage read first; scrape runs only with `--confirm-no-paid-charge`
  AND explicit owner evidence (`--owner-included-credits-evidence`) of included,
  non-billed credits with no autobilling (a positive balance is NOT proof); at
  most one attempt, in an isolated `finalization/teststate-benchmark/` ledger
  (per-cycle 1 / per-day 10 / concurrency 1 / 60-min cooldown). Failure classes
  are a strictly allowlisted generic code (`rendered_markdown`, `blocked`,
  `js_shell`, `quota_deferred`, `private_url_denied`, `timeout`, `error`, ...);
  no raw MCP/account text, token or URL query and no credit value is persisted.
- **Cutover**: durable owner-only backup of displaced code + data + jobs;
  non-blocking runtime locks (abort if busy); pauses/resumes only the two
  recorded owned jobs, checking every native return code and reading back the
  exact status (unknown status or a control failure aborts; a job already
  paused is never falsely re-resumed); verifies job name/schedule/script/
  deliver/no_agent and the full inventory; compares protected state as a FULL
  before/after inventory (additions/removals/changes) with only an explicit,
  named lock/ledger allowlist; restores `user_config.json` byte-for-byte
  atomically on ANY later failure and only ever applies the explicit retrieval
  diff; detects import-relevant stale live modules and aborts (nothing deleted
  without approval); installs only manifest files; runs `configure validate`,
  `doctor`, and the no-send fixture; on failure atomically restores displaced
  code, retains new files, records them and marks the rollback incomplete.
- **Publication**: requires a verified cutover artifact whose installed
  manifest sha256 matches this release; manifest hash + forbidden-path + secret
  scan; dedicated clean checkout; hidden/repo-owned and ALL unexplained stale
  tracked paths abort (no deletion); staged diff limited to the manifest; plain
  `git push` (never `--force`); remote SHA readback equals local HEAD;
  repository privacy is verified with `gh` BEFORE the push and read back AFTER
  (gh unavailable/error or a non-private repo aborts, never `ok`).

## Remaining blockers / deferred (honest)

- **Native render fallback remains unavailable**: no constrained public-only
  host renderer is exposed by the supplied references; the generic shared
  browser (owner cookies/session/SSRF surface) is deliberately not reused. Not
  part of this benchmark; reported as an explicit capability blocker.
- **`firecrawl_schema_verified`** must be set by the parent only after the real
  benchmark confirms the configured tool/schema; the harness never guesses it.
- **Local attempt caps are not provider credits.** The parent must verify the
  account's free/paid allowance before the first real call; the benchmark
  fails closed without the explicit non-paid assertion.

## Freeze

Preparation is complete inside `candidate/` only. No live execution, no
network, no scheduler/GitHub write, no notification. Frozen pending parent
review, real benchmark, cutover and publication.
