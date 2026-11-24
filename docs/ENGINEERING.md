# Engineering

This document is the developer and host-owner view of Hermes Proactive Secretary. It complements [Installation](INSTALLATION.md) and the read-only [portal behaviour guide](NATIVE-PORTAL.md). It contains no owner-specific identifiers, accounts, production summaries or private filesystem roots.

The package version is **1.1.6**. The plugin id is `hermes-proactive-secretary`; the public repository is `Akuyumu/hermes-proactive-secretary`. The retired legacy plugin id `hermes-proactive-portal` is not read or registered. On first plugin load, owner-only state is copied once from the legacy data root into the new root, without modifying the legacy tree; old owner-facing job names are retired through the ownership ledger.

## Repository layout

The repository root **is** the plugin package Hermes loads: `__init__.py` is the
entry point and defines `register(ctx)`. `secretary/` holds the Hermes-facing
surface modules registered by that entry point. `src/proactive_pilot/` is the
engine. `runners/` contains the shipped cron scripts. `scripts/` is developer
tooling excluded from the release archive, and `docs/` holds documentation.

Root runtime modules (`paths`, `cron`, `runtime_gate`, `state_import`,
`runner_install`) must not be relocated: `runners/*.py` execute as scripts with
the plugin root on `sys.path` and import them by top-level name.

## Architecture ownership

The plugin is a real Hermes plugin. Its `plugin.yaml` registers:

- a native CLI command group (`hermes secretary`);
- native tools (`secretary_onboarding`, `secretary_context`, `secretary_responsibility`, `secretary_propose_notice`);
- a `pre_llm_call` hook;
- an operational skill and ordinary native Desktop settings contributions.

The plugin owns its configuration, policy copies, preferences, reminder registry, scheduler/runner ownership ledgers and preparation artifacts. It does **not** own Hermes's profile memory, permissions, connector credentials or approval system. There is no parallel memory or permission engine.

Runtime state is owner-only under the active profile's `plugin-data/hermes-proactive-secretary/`, unless an explicit operator override is used. Foreground and scheduled work resolve the same active profile. Missing profile context fails closed rather than borrowing the launch profile.

## Native session versus collectors

Two lanes are distinct:

1. **Collectors and deterministic runners.** The monitoring and reminder jobs are native `--no-agent` script jobs. The reminder runner is deterministic and makes no model call. Monitoring may invoke a host-owned model lane for discovery and judgment. Optional private preparation is a separate opt-in script job.
2. **The native portal session.** One optional, plugin-owned Hermes cron job runs as a normal native agent. It inherits the owning profile's identity, tools, connectors, skills, memory and standing approval rules. It is a separate scheduled run, not a literal foreground session id.

The portal's native final output stays local. When something warrants the user's attention, the session uses the guarded `secretary_propose_notice` tool to queue a proposal for the existing monitoring lane. A proposal is not completion or delivery. Notice delivery remains subject to quiet hours, cooldowns and the optional daily cap. The portal must not send through two paths.

Setup, settings saves and registration do not arm the portal. Preview and enable are explicit and require a schedule; there is no hidden default cadence. Pause stops later runs but is not a proven hard cancellation of a run already in progress in the native transport.

## Candidate architecture walkthrough

This walkthrough describes the candidate tree as it exists. It is explanatory,
not a new contract or a promise of live behaviour.

### Components and data flow

```text
foreground private chat
  -> secretary_followup (create/list/inspect/stop)
  -> followups.json (owner-only, bounded, shared by the portal)

native script jobs (--no-agent)
  collectors + reminder runner
  -> evidence, reminders and state
  -> optional preparation runner (explicit opt-in)

native normal-agent portal job
  (preview/enable require an explicit schedule)
  -> reads the owning profile's sources, responsibilities, memory and follow-ups
  -> prepares private local work
  -> secretary_followup (due/record_check)
  -> notice request: secretary_propose_notice or queue_notice
  -> portal_notices gates (quiet hours, cooldown, optional daily cap)
  -> local final output; any external send stays approval-required
```

### Entrypoint and native schedule

`__init__.py:45` is the registration boundary. `register(ctx)` binds the native
CLI context, optionally registers the configured-only model lane, registers the
skill, the conversation tools and the proposal tool, and registers the
`portal` CLI group. Registration does not read private sources, send a
message, arm a job or change access. Optional surfaces fail without taking down
the deterministic CLI.

The native portal lifecycle lives in `secretary/secretary_portal.py:410`. Its actions are
`preview`, `enable`, `status`, `pause`, `resume` and `remove`. `preview` and
`enable` require a schedule; there is no hidden default cadence. The job is a
normal native agent job, not a `--no-agent` script job, and it pins
`deliver=local` plus `context_from=["self"]`. Setup does not arm the portal.
Pause stops later runs but is not a proven hard cancellation of a run already
in progress in the native transport.

### Independent initiative

The work contract in `secretary/secretary_portal.py:29` tells the scheduled session to
inspect accepted responsibilities, standing instructions, authorised sources,
prior progress and existing artifacts, and to investigate and prepare private
work proactively. It may consult active follow-ups through the typed tool; when
there are none, or the tool is unavailable, it uses its original initiative
independently. That independence does not grant new authority, sources,
spending, credentials or external sends, and follow-ups never create a
schedule or mutate the per-watch prompt.

### Shared follow-up tool and data

There is one tool, `secretary_followup`, defined in
`secretary/secretary_followups.py:34`. Foreground actions (`create`, `list`, `inspect`,
`stop`) still require a current private human turn and a matching evidence quote
where one is claimed; background, shared or stale turns are refused. Desktop
create and stop therefore require a captured private owner turn, or an explicit
request when no quote is available. Filing is **agent-inferred and reversible**:
there is no phrase-list gate, because a follow-up is a small text addition to the
plugin rather than a destructive or scheduled action. The only veto is a narrow,
literal retraction in the quoted turn ("don't watch", "no need to watch", "never
mind", "forget it"). Every entry records `filed_by` (`owner` for a verified turn,
`agent` for an explicit request), the bounded `evidence_quote` and the
`source_ref`, all visible in `list`/`inspect`. Creation refuses when both the
quote and the explicit request are empty, and never creates a schedule or cron
job. Stopping keeps its explicit-intent and exact-target requirements unchanged:
it is destructive, so it is deliberately not relaxed to match creation. The
approval-prompt fallback remains disabled because the current host cannot prove a
fresh per-request owner decision bound to the exact operation, profile and
payload; if that capture is unavailable, the action fails closed. Portal actions
(`due`, `record_check`, `queue_notice`, `resolve_notice`) require the same
verified owned portal execution identity used by the notice tool.

The durable store is `src/proactive_pilot/followups.py`, persisted as
`followups.json` through the shared state store with owner-only permissions,
atomic writes, a lock, an explicit schema and a monotonic version. It holds at
most 20 active follow-ups and bounded text, hints, evidence and seen
fingerprints. It does not schedule checks, call models or touch the network.

A scheduled portal run may also create its own watch with the same tool. That
path forces `filed_by="agent"`, requires a nonblank `check_hint` and reason,
caps creations at three per execution and mints an expiry (14-day default, 60-day
maximum). Selecting due work closes an expired active watch with
`status="closed"`, `close_reason="expired"` and `closed_at`; the row is retained
and legacy rows without `expires_at` never expire.

Staged owner-approval drafts live under the plugin data root in
`src/proactive_pilot/drafts.py`: `drafts/pending/<dr_id>/manifest.json` plus a
Markdown/text body, with terminal `approved/`, `discarded/` and `expired/`
siblings. The directory tree is the only state (no index, lock file or second
database). Portal runs may stage and list pending ids/titles; only a foreground
session may show, approve or discard. Approval moves the directory and returns a
local receipt with `executed=false`; it never performs the staged action.

The delivery ledger lives in `src/proactive_pilot/deliveries.py` and is
persisted as `deliveries.json` through the same shared state store: owner-only
directory and file modes, atomic temp-file write plus `os.replace`, a dedicated
`deliveries.lock`, strict reads that refuse symlinks and malformed shapes, at most
50 rows, 14-day retention, 350-character message text and at most 8 evidence
refs. It records only emissions that actually reached the delivery boundary.
Requested-reminder dispatch is not ledgered in this release. A ledger failure is
diagnostic only and never blocks or rewrites a notice.

A **one-shot** follow-up is a single semantic event. Queueing freezes it before
the enqueue attempt and its event key depends only on its durable id, so a
restart cannot create a second send path. Only a positive `delivered` receipt or
an exact owner acknowledgment may close it. An `emitted-unconfirmed` one-shot
exposes a notice receipt reference only after the portal queue records that
state; it can be closed only by an explicit owner acknowledgment naming that
exact reference in the current private owner turn. That records an
`owner_confirmed` outcome, never transport verification, frees the pending slot
and does not resend. Replaying the same acknowledgment is idempotent; a
different reference is refused. Otherwise `emitted`, `attempted` and `unknown`
leave it awaiting and never trigger a resend, and the shared tool refuses to
assert `delivered` without a verified receipt. Example: "let me know when the
consultation reopens" creates one one-shot watch, and repeated scheduled runs
do not enqueue it twice.

An **ongoing** follow-up can notify once per genuinely new match. Its notice
event key includes a bounded match fingerprint, and already-seen fingerprints
are suppressed. Example: "keep an eye on the timetable for cancellations"
remains active across runs; a new cancellation fingerprint may be queued once,
while the same fingerprint is not repeated.

### Gated notice path

Notice proposals go through the existing `portal_notices` transport rather than
a second sender. The transport validates a bounded proposal, and the existing
monitoring gates decide whether anything is emitted. `queue_notice` reports
`queued`, `already_queued`, an ambiguous `enqueue_unknown`, or a refusal; none of
these is delivery. The follow-up tool refuses to resolve a notice as
`delivered` because no verified delivery receipt exists in this candidate.
A proposal or queue entry is therefore not completion, not an acknowledgement
and not proof that a message reached a device.

### Developer scripts, installed runners and export shim

Repository developer tooling lives under `scripts/`, including
`scripts/release_export.py`; the exporter excludes its own `scripts/` tree and
`tests/`. The `runners/` tree contains the shipped runner templates. The scripts
the native scheduler runs are copies installed into the active profile's
`scripts/` directory after an explicit setup; they are not the repository
developer scripts. The exporter builds a deterministic code-only archive and
its scan/doctor checks validate packaging, entrypoint import/registration and a
synthetic host doctor. That is not live activation, connector access, model
availability or delivery.

The root `release_export.py` is a compatibility shim only. It re-exports
`scripts/release_export.py`, including its helpers, and preserves the same
module identity; new maintainer automation should use the relocated module.

### Tests

The `tests/` tree is offline and fixture-based. Representative coverage includes
portal lifecycle and readback (`test_portal_lifecycle.py`), notice transport and
gating (`test_portal_notices.py`, `test_notification_contract_053.py`), shared
follow-up behaviour (`test_followup_integration.py`,
`test_portal_tool_schema.py`), installed-candidate checks
(`test_installed_lastmile.py`, `test_release_doctor_runtime.py`) and the wider
source, reminder and responsibility suites. These tests use simulated chat
contexts and synthetic profiles. A passing run is not live behaviour.

### Limitations

- Simulated chat tests are not live and do not establish live chat behaviour.
- The current audit has not established native per-notice acknowledgement.
- The current audit has not established reliable Desktop foreground metadata.
- Do not infer delivery from a coarse job outcome such as enabled, running or
  succeeded; only explicit notice evidence can support a delivery claim.
- A proposal, queued follow-up notice or local final output is not delivery.
- Pause is not a proven hard cancellation of an in-progress native run.
- Packaging scans and doctor checks validate packaging only, not live services,
  models, connectors, Desktop state or devices.
- Concurrent fix internals are intentionally not documented here because they
  may change.

## Host prerequisites

### Configured-only auxiliary contract

Model-dependent preparation and other strict tasks require a host that exposes the configured-only auxiliary contract — specifically an auxiliary task registration/read path honouring `fallback_policy="configured_only"`.

When that contract is absent, the plugin must report the lane as unsupported and make no model call. It must not fabricate an artifact, a fallback ladder or a model-outage notice. Current tooling deliberately avoids blanket route fallback. A change must not reintroduce an implicit main/global/chat escalation.

### Native list readback

Portal reconciliation is bounded by what the native job listing actually exposes. Report state honestly:

- **Mode** — a missing mode is fail-closed: `enable` must refuse rather than infer native agent mode.
- **Prompt** — a stored single-line prompt may be exposed as `Prompt`. A multi-line prompt is not collapsed, rewritten or truncated merely to fit that row, so it remains inconclusive there.
- **Context from** — nonempty native context references appear as `Context from`, for example `self`; an absent self/context reference is inconclusive.

If the listing does not expose a field, `preview` must mark it inconclusive and must not present settings as applied state. Missing mode fails enable; absent prompt or self-context is not evidence of another mode or context. Verify the persisted record through the native job surface where available.

### Approval and MCP semantics

The portal inherits native MCP trust and approval behaviour. Native MCP tools retain full trust outside `cron_mode`; consequential external effects still require the applicable foreground approval. The work contract prompt is guidance, **not enforcement**, and the portal is not a security sandbox. Any external send, purchase, publication, booking or calendar/task mutation remains an approval-required action.

## Host compatibility patch

Some Hermes versions need a host-side compatibility update for the strict configured-only auxiliary lane. The patch included with this package is [docs/compatibility/hermes-host-portal.patch](compatibility/hermes-host-portal.patch). It was reviewed against Hermes base:

```text
2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19
```

This is not a universal or zero-core-change claim. Do not copy or apply the patch to a different or newer checkout without re-review.

### Safe application

Before applying, confirm all of the following:

- the host source tree is clean and owned by the operator making the change;
- you have explicit authority to change the host;
- the expected base commit matches the reviewed base above;
- a rollback copy or owned diff is available.

Then check first, without writing:

```bash
git -C <hermes-source> apply --check <path-to-docs/compatibility/hermes-host-portal.patch>
```

Apply only after a clean check and explicit approval:

```bash
git -C <hermes-source> apply <path-to-docs/compatibility/hermes-host-portal.patch>
```

Record the exact files changed. A local host patch must survive future Hermes updates: after each host upgrade, re-check compatibility and re-apply only a re-reviewed patch. Do not assume the change carries forward.

### Reverse and rollback

Reverse only the exact owned diff. Check first:

```bash
git -C <hermes-source> apply --reverse --check <path-to-docs/compatibility/hermes-host-portal.patch>
```

Then reverse when it is safe:

```bash
git -C <hermes-source> apply --reverse <path-to-docs/compatibility/hermes-host-portal.patch>
```

Never use `git reset --hard`, `git clean`, or a blanket checkout to undo this change. If the working tree has diverged, stop and reconcile the owned diff manually rather than discarding unrelated work.

## Release and packaging

Package version and metadata are authoritative in `plugin.yaml` and the release-export inputs; this documentation does not set them. `main` is the production/stable branch and the source of released installs; `dev` is the active development branch. Release tags mark tested `main` commits. For an immutable pin, resolve the immutable 40-character commit SHA and pass it through the native `--ref` argument.

Repository developer tooling lives under `scripts/` (for example `scripts/release_export.py`) and is excluded from the exported archive; the old root `release_export.py` is a compatibility shim only. This is distinct from the shipped `runners/*.py` templates and from the profile-installed scripts under the active profile's `scripts/` directory, which the native scheduler runs after an explicit setup.

The exporter builds a deterministic code-only archive and excludes tests, runtime state and `.build-context`. Its scan and doctor checks validate packaging only. They do not establish live connector access, model availability, message delivery or Desktop/device behaviour. Do not claim that every service or source always works, and do not infer universal third-party availability from a green packaging run.

## Responsible changes

Preserve existing approvals, private state and profile scope. Do not broaden permissions or connectors implicitly, do not introduce a separate restricted assistant, and do not weaken sanitiser, export or privacy boundaries to make an artifact pass. Keep documentation honest about unavailable, failed, deferred and truncated coverage.
