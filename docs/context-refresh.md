# Opt-in assistant context refresh

The secretary can keep its existing bounded context snapshot useful without a
new memory backend, a regex extractor or another model call. This is an explicit
opt-in, assistant-mediated refresh of the same profile-local, expiring snapshot
that the scheduled pipeline already consumes.

## Behaviour

- The dedicated boolean setting `context_refresh` is **off by default** in the
  canonical typed user configuration and the native plugin form. Only an
  explicit boolean `true` enables writes; missing, ambiguous or non-boolean
  values deny them. Native form, CLI/file configuration and policy projection
  all use this one field.
- On an eligible normal private conversation turn, the existing `pre_llm_call`
  hook may append short, stable guidance. It never includes the user's message,
  history, raw excerpts or secrets. Existing safety context keeps its priority;
  refresh guidance is appended after it, while an actual pending onboarding
  continuation may defer the hint. Background, completion, cron, group, shared
  and slash-command turns receive no guidance. Native Desktop background-process
  and async-delegation completion envelopes are also excluded, including
  multi-part text input, because they are events rather than human turns.
- The native `portal_context` tool exposes only `list`, `show`, `update` and
  `revoke`. Its schema is closed and bounded; `list` is metadata-only, `show`
  reads one entry back, and errors are generic and text-free.
- An inferred update is always stored as `advisory: true`. The tool cannot
  declare owner authority, owner consent, a new account or a new source. Only
  the `assistant:knowledge` and `assistant:memory` provenance labels are
  accepted for inferred entries. Owner-declared entries may be listed or shown
  but cannot be modified or revoked through this tool; the existing CLI remains
  the explicit owner-authority path.
- Duplicate or unchanged facts are skipped rather than renewed. New facts still
  obey the existing entry, text/total-size and tombstone caps. Expiry remains
  mandatory and bounded; revocation remains a durable tombstone.
- Writes fail closed when refresh is disabled, the profile is paused or its
  preferences are corrupt, or the snapshot is corrupt. Existing CLI revocation
  and expiry behaviour is unchanged.
- There is no network access, model call, subprocess, history mutation or
  automatic whole-memory/Library/connector import.

## Limits

- Model compliance is an explicit residual limitation: the assistant may skip a
  relevant refresh or propose a poor entry. The tool validates safety and shape,
  not semantic quality.
- No source is read to discover facts. Refresh only uses context already
  available in the current conversation.
- Pausing or disabling the setting stops new writes; it does not erase existing
  entries. Entries expire normally. `portal_context revoke` withdraws one
  assistant-advisory entry permanently; owner-declared entries are withdrawn
  only through the existing CLI.
- Scheduled consumption is unchanged and only sees entries that are live,
  unexpired and validated.

## Settings and controls

Enable `context_refresh` in the plugin settings. Disable it, or pause the
secretary, to stop new assistant-mediated writes. Use `/portal pause` for a
temporary stop and the existing CLI for inspection, revocation and profile
maintenance. Each profile keeps its own snapshot and preferences.
