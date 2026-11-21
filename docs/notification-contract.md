# Notification contract (v0.5.3)

This document defines the low-noise attention contract for proactive notices. It
is a source document in the repository and is not shipped as a package-relative
doc; the README links it at the canonical repository.

## Delivery vocabulary

- **Configured**: a source, scope or notification route is present in validated
  configuration. This is permission, not proof of a read or a send.
- **Emitted**: the scheduled process wrote a permitted message to stdout.
- **Delivered**: the host or transport confirmed receipt. The plugin does not
  claim this from stdout alone.

A suppression decision prevents an emission; it never rewrites history or
fabricates a delivery. Ambiguous emission remains terminal for that artifact and
is not blindly retried.

## Readiness notices

A queued preparation artifact is not itself a notification. The notice must give
the owner genuinely useful task-specific content derived only from the accepted
responsibility (goal, required decision/acceptance note, deadline). A generic
"a private draft is ready" status line is not sufficient.

Owner-facing text is bounded, plain prose. Raw responsibility/artifact ids,
paths, refs, handles, generation counters, control characters and injected
instructions are removed or omitted. Detailed linkage remains structured and
available on demand instead of being pasted into chat.

## Semantic attention identity

The persistent attention key is a hash of the normalized owner outcome:

1. desired outcome (owner goal),
2. acceptance criterion (required decision), and
3. deadline.

Artifact hashes, artifact refs, generation counters, queued/checked timestamps,
source polling versions, evidence ordering and cosmetic evidence rewording are
deliberately excluded. They are churn, not meaning. Missing values stay empty
rather than being guessed, so uncertainty cannot invent a material change.

A new responsibility, or a materially changed owner goal/decision/deadline, may
qualify for a new notice. Repeated ticks, restarts and regenerated drafts for an
unchanged responsibility do not. The attention key is written atomically with the
existing one-shot claim, so claim token/generation/source/lease fencing and
unknown-emission non-retry behavior are preserved.

## Envelope and preferences

The ordinary owner controls still apply before any claim: current preparation
enable/mode/scope/source consent, quiet hours, pause/snooze, cooldown and the
daily ceiling. The daily ceiling is a maximum, never a target. Per-item explicit
suppression rules and scoped snoozes are rechecked in this path; `notice_allowed`
alone is not sufficient. Requested reminders and genuinely important alerts are
not disabled by this contract.

## Upgrade and migration limits

Legacy state has no semantic attention record. Migration seeds suppression only
from historical evidence that names the **current** draft: an emitted state row
whose signature contains this artifact's filename, or a terminal
delivered/unknown receipt bound to this exact artifact ref. Seeding does not change
generation, reset a claim, delete history or claim delivery.

Evidence about a *different* draft is deliberately not treated as proof. Seeding it
would mark the present owner outcome as already announced and suppress a genuinely
changed outcome permanently — the worse failure. The cost is bounded and small: at
most one notice on the first tick after upgrading, for a draft generated after the
last announcement, and then silence until the outcome materially changes. Corrupt or
unreadable history seeds nothing rather than guessing.

A version upgrade cannot recover history that was never persisted, and it does not
convert an unconfirmed emission into a delivery. Corrupt or unknown state fails
safely rather than re-arming everything.

## Source health and evaluator failures

Routine transient source errors stay internal. A critical, persistent,
owner-actionable source incident alerts at most once per unresolved incident; the
incident id, not cooldown expiry, exception wording or a new scheduler process,
decides. Recovery requires an authoritative attempted success, after which a
truly new incident may alert once. Generic evaluator failures notify once per
failure streak and remain armed while the envelope is suppressed. This contract
does not add a self-healing or retry system.

## Continuity after delivery

A notice emitted into the ordinary assistant conversation remains resolvable on a
later interactive turn through an owner-only delivery ledger
(`deliveries.json` beside the other profile state). Recording happens only when
text is actually handed to the delivery boundary; blocked, suppressed,
queued-but-unemitted and diagnostic paths record nothing. The delivered message
is byte-identical with or without the ledger.

The `pre_llm_call` hook never injects notice text. The host passes no chat id or
chat type to that hook, and a single platform value covers both direct messages
and groups, so injected continuity is deliberately content-free: only a recency
count, the newest timestamp and the on-demand read surface. The owner can resolve
an old referent with `hermes secretary deliveries --since <ISO> --limit <n>`.

Ledger rows stay `emitted-unconfirmed`. Remembering an emission is not a delivery
receipt and does not change any delivery gate.

## Rejected alternatives

- **Cooldown as deduplication**: cooldown only spaces messages; it re-arms after
  the interval and produced repeated unchanged notices.
- **Daily cap as a target**: the cap is a ceiling, not an invitation to fill it.
- **Artifact hash/ref/generation as semantic identity**: regenerated drafts change
  these without changing the owner outcome.
- **Blanket disabling preparation notices**: this would suppress genuinely changed
  useful outcomes and requested work.
- **New model loop or forced main-model call**: a deterministic owner-outcome
  policy is sufficient; any evaluator extension stays on the strict assigned
  `ctx.llm` lane.

## Regression coverage

- `tests/test_notification_contract_053.py` — legacy migration, repeated
  generations/restarts, material change, quiet/cap/explicit preference
  suppression, unknown non-retry, identifier-free rendering, claim fencing and
  source-health incident/recovery proof.
- `tests/test_main_final_paths.py` — production ready-notice seam and packaging
  version assertions.
- `tests/test_preparation.py` — claim/generation/hash dedupe and stale receipts.
- `tests/test_source_health_production.py` — scheduled retry/restart behavior,
  one alert per unresolved incident, and a new incident after authoritative
  recovery.
