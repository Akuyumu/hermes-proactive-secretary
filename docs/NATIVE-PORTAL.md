# Native portal job

The native portal is one optional, plugin-owned Hermes cron job that runs as a
normal native agent. It is separate from the plugin's default collection jobs.
Nothing is created at import, registration or settings-save time: you must run
preview and then enable explicitly. Editing plugin settings does not silently
create or update the job.

## Opt-in command flow

Preview first. A schedule is required for preview and enable; the plugin adds no
hidden cadence of its own.

```bash
hermes portal job preview --schedule "<schedule>"
hermes portal job enable --schedule "<schedule>"
hermes portal job status
hermes portal job pause
hermes portal job resume
hermes portal job remove
```

`preview` reports both `requested_pins` (the values this operation plans to use)
and `effective_pins` (honest live/readback state). `status` reports live actual
pins only; it does not present current settings as if they were already applied.
The work contract prompt is reconciled as part of the job shape but is not shown
as a small settings pin. Run `enable` again to reconcile an existing owned job
with changed settings. For an owned job, `enable` compares the current prompt and
any exposed native pins against the requested values, and recreates only that
exact recorded job when its shape changed; it never adopts, overwrites or removes
a foreign same-named job. `preview` reports a changed prompt as `would-update`
without writing anything. Repeated `enable` with an unchanged owned job is
idempotent and does not duplicate or churn it.

Reconciliation is bounded by the native readback. The native listing now exposes
a stored single-line prompt as `Prompt` and nonempty native context references as
`Context from` (for example `self`). A multi-line prompt is not collapsed,
rewritten or truncated into that row, so it remains unverified there. When the
native listing does not expose the stored prompt, `preview` and `enable` report
it as unverified and `enable` refuses to claim the job is current instead of
trusting a stale or missing ownership ledger. Verify the persisted prompt through
the native job record where that field is exposed.

`pause` stops later runs. It is not a proven universal hard deadline or
cancellation of a run already in progress in the native transport. `resume`
clears the paused state but does not invent a missing job or permission.
`remove` deletes only the plugin-owned job after matching its recorded identity.
Durable memory and other profile state are retained after removal. Cleanup of
pending notices still needs acceptance.

## Model and reasoning snapshot

On preview/enable only, the plugin reads each needed setting once through the
native configuration reader and freezes those values for that operation. Later
re-reads do not occur during the same command.

- An explicit `--provider` and/or `--model` keeps the existing native CLI
  precedence. If either is supplied, the configured primary pair is not used to
  fill or replace that route. Supplying only one leaves the other to normal
  native inheritance.
- If neither is supplied, the plugin uses **both** configured
  `primary_provider` and `primary_model` when both are nonempty. Both empty
  means ordinary native inheritance, not a hardcoded model.
- A half-configured primary pair, a wrongly typed value, or another invalid
  setting fails before backend construction, job listing or job mutation.
- An explicit `--reasoning-effort` wins. Otherwise the optional
  `portal_reasoning_effort` setting is used when it is a valid nonempty level;
  blank means native inheritance. Accepted native levels are `none`, `minimal`,
  `low`, `medium`, `high`, `xhigh`, `max` and `ultra`. Unknown levels, wrong
  types and control characters fail before backend work.

The monitoring auxiliary route keeps its existing routing rules. The portal
snapshot is not that route. A configured plugin backup/fallback pair is
auxiliary-only; a pinned native portal cron job does not inherit it. If a native
cron job or native auxiliary pin already sets its own provider, model or
fallback, that native pin wins and the plugin does not change it.

## Delivery and the monitoring lane

The portal job manages local native final output. It may use the guarded
`portal_propose_notice` tool to queue a proposal for the existing monitoring
lane. The monitoring lane must already be enabled before notices can be
delivered; a proposal is not completion, delivery or a guarantee that a message
was sent.

Plugin-owned notice delivery controls are narrower than general native tool
authority. The portal is not a sandbox and imposes no all-tool cap on the native
agent. Native MCP tools retain full trust outside `cron_mode`. Any external send,
purchase or publication still requires the applicable foreground approval for
that action.

Normal owning-profile native Hermes capabilities, memory and approval behaviour
remain available to the job. It inherits the same owning-profile identity, tools,
connectors, skills, memory, and standing permission and approval rules as an
ordinary Hermes session; there is no portal tool allowlist. It is still a
separate scheduled run: it is not the same foreground session id, and this
document does not claim vendor parity, perfect memory or a sandbox. Native memory
use and adaptation have bounded foreground proof only; integrated scheduled
memory, correction and follow-up behaviour is not yet runtime-qualified in an
integrated scheduled run. Do not infer an "always aware" state or organic
learning proof from this document.

## Native self-context

The native CLI expresses a job using its own context as the boolean
`--continuity` flag on `hermes cron create`. The plugin emits `--continuity`
exactly once when the requested context normalises to `['self']`, and emits no
context flag when it is empty. There is no `--context-from` CLI flag and the
plugin does not invent one. Because this backend cannot express an arbitrary
upstream job reference through the CLI, any non-self context reference is
rejected before a job is created, updated or removed. The persisted native job
and the ownership ledger record the normalised self context as `['self']`. The
native text listing reports nonempty context references as
`Context from: <comma-joined ids>` (for example `self`).

## Work and follow-up contract

The portal is instructed to act as the owning profile's private secretary rather
than a notification generator. On each native run it should:

- inherit the same owning-profile Hermes identity, tools, connectors, skills,
  memory, and standing permission and approval rules as an ordinary session;
- inspect accepted responsibilities, standing instructions and authorised
  sources available in its owning profile, within current authority;
- proactively seek useful information in already authorised sources and native
  capabilities, not only fixed collector candidates or accepted tasks;
- eagerly investigate, prepare private useful artifacts, suggest actionable next
  steps, verify and persist progress, and notice material changes within
  standing authority; a new explicit responsibility is not required for every
  useful action;
- use changed evidence as the main signal and avoid repeated work on unchanged
  inputs, while allowing a new preparation opportunity to be useful without a
  source revision;
- execute work with native tools and read the actual result back before claiming
  success;
- carry progress, evidence and any next due follow-up in native memory or
  existing work artifacts, without creating a parallel memory, preference,
  scheduling or permission engine;
- apply explicit corrections, preserve standing obligations and temporary
  scope, and not treat thanks, silence or non-response as instructions to drop
  an obligation or as learned dislike;
- leave approval-required actions unexecuted and surface a concrete pending
  blocker with the exact approval needed;
- use `portal_propose_notice` for user notices, keep the native final local,
  and stay meaningfully silent when nothing useful or changed warrants action.

This contract does not create authority, sources or spending, and it is not
enforcement or a sandbox. It does not imply that every MCP write is gated: native
MCP tools retain full trust outside `cron_mode`, and the existing approval path
still governs consequential effects. Skills and workdir remain optional native
pins, not requirements of the contract.

## Daily cap

The optional `daily_normal_cap` setting limits ordinary discretionary messages:

- blank: no deterministic daily count limit;
- `0`: stop ordinary discretionary messages;
- an explicit whole number: use that number as a ceiling, never a target.

Urgent evidence and reminders you explicitly asked for keep their existing
exceptions, so this is not a cap on every message.

## Reversible removal and archive boundary

To stop and remove the portal explicitly, run `hermes portal job pause`
and then `hermes portal job remove`. The plugin's broader
`/portal disable` path also pauses and removes only its own owned jobs and
runners. Neither path deletes durable profile memory.

A built archive may be staged for review and rollback. Staging is not
installation or enablement; it does not arm a job, change native configuration
or start delivery. To roll back, restore the previously reviewed archive and
restart Hermes under your normal procedure. Do not auto-enable or auto-arm any
job as part of staging.

This document describes the source implementation. It does not claim that a
release is ready, installed or live.
