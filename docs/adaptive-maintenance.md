# Adaptive maintenance (candidate)

This document describes the adaptive-secretary maintenance seams. It adds no
new access: every route remains a previously approved public route, source content
is untrusted evidence, and private/paid/credentialed expansion is never automatic.

## Per-source public truth

Status/doctor no longer reduce public health to the newest `last_success_at`.
Each enabled ordinary registry source is evaluated with its own cadence and the
durable per-source incident record. Adaptive coverage rows are evaluated too.

- A recent successful read is fresh.
- A source inside its cadence with valid cached evidence is healthy
  (`cached-valid`), not stale.
- Budget deferral, per-run cap deferral, rate limiting, transient transport
  failure and an intentional LTA transport-gate skip are neutral observations:
  they neither make every public source "down" nor pretend a read happened.
- An adaptive plan with no supported route is explicitly `unsupported` (an
  unresolved limitation), not a healthy empty result.
- The aggregate is `partial` when some sources are healthy and others failing or
  unsupported, `unavailable` only when every configured source is unreadable.
- Calendar/task/private truth fields and their existing vocabularies are
  unchanged.

## Automatic reversible replacement (public only)

A persistently defective ordinary or adaptive public source can be replaced
automatically ONLY when standing public-maintenance consent and the existing
broad public discovery route are both enabled. `feeds_enabled` alone grants no
autonomy; neutral new installs stay inert. The seam runs in scheduled collection.

Rules:

- A same-topic search hit is not an equivalent. Candidates come from the existing
  bounded, vetted broad-discovery path (`discovery.py`) and its public DNS/TLS
  checks, not from fixed GitHub/Reddit substitution.
- Before any search, the outbound query is rechecked by the existing structural
  public-query privacy boundary. Private facts never become maintenance queries.
- A strict zero-tool structured decision on the existing model lane must verify
  SAME purpose, authority class and date/deadline scope against fetched evidence.
  If the strict lane is missing or uncertain, nothing is adopted. Official
  SMRT/NTU/weather sources require an official authority match on an
  already-vetted official host; forum chatter cannot replace them.
- Adoption also requires a real bounded fetch of the candidate with fresh,
  non-cached, non-empty usable evidence. That evidence/cache is preserved on the
  adopted record, so the current cycle can still use it.
- Search, HTTP, shared source-attempt and residual wallclock budgets are checked
  on every attempt; a failure or deferral is recorded, not hidden.
- Rate limited, timeout, deferred, budget and per-run-cap observations never
  trigger replacement.
- One verified change per cycle and a cooldown bound retries; attempts persist.
- The old record is parked, not deleted: its cache, provenance, failure history
  and seed identity remain. Stable opaque lineage ids (not list positions) store
  old/new ids and URLs, interest, purpose/authority, reason and evidence time.
- The current active watch/goal and current scope/pause/revocation/snooze state
  are rechecked immediately before repair/read.
- One bounded notice per actual change uses the ordinary envelope
  (quiet/pause/cooldown/shared five-per-day cap); the cap entry is recorded
  before stdout so a crash cannot exceed the cap. Health/urgent evidence keeps
  priority. A missing strict model lane does not block displaying an already
  deterministic pending notice.
- Reversal: `hermes secretary feedback --feedback source-list` lists stable
  references; `--feedback source-dispute --id <stable-ref>` performs a locked,
  idempotent exact restore and permanently blocks that same rejected replacement.
  Original failure history is preserved, not reset.
- An unsupported/unavailable route remains an explicit unresolved limitation.
  Automatic replacement is not claimed where no approved public route/evidence
  exists.

## Feedback-responsive interests

Explicit corrections (`watch-change`/`watch-feedback`) revise the next plan and
acquisition. Explicit suppression or an active scoped snooze on the same
watch/interest also blocks the next acquisition and matching evidence without
retiring the watch. Temporary snoozes expire by clock, so the same watch resumes
automatically. Conditional rules (for example "unless exceptionally fitting")
are not blanket suppressions; inferred preferences remain advisory. "Thanks" and
silence are no-ops. Explicit watches last until an explicit correction. Inferred
goals may be blocked or deprioritised, never promoted to explicit watches.

## Configuration proposals

Operational evidence can produce one typed proposal with an exact allowlisted
key and before/after value, a reason, evidence and an expiry. Identical unchanged
proposals are deduplicated by a stable id. A proposal is never a write: it does
not change scopes, permissions, model route, budget, caps, pace or credentials,
and no arbitrary configuration key can be executed.

Proposals are presented inside the SAME ordinary attention envelope as alerts and
replacement notices (same five-per-day cap, cooldown, quiet hours and pause).
Approval must happen in the foreground through the existing validated writer and
backup path. `--feedback config-proposal-list` reads them;
`--feedback config-proposal-dismiss --id <id>` records an exact dismissal.

## Limits

This candidate has synthetic tests only. It does not claim live delivery, live
model/search qualification, real connector reads or device verification. Public
retrieval still contacts external services when enabled; read-only does not mean
offline. Provider status, transient failures and unsupported routes are recorded
honestly rather than papered over.
