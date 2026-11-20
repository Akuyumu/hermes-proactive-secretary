---
name: proactive-secretary
description: "Use when a user sets up, runs, tunes, pauses or troubleshoots the proactive secretary plugin (proactive alerts and requested reminders)."
version: 1.0.3
author: Hermes Plugin Community
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [secretary, proactive, notifications, reminders, cron]
    related_skills: []
---

# Proactive Secretary

## What this is

`hermes-proactive-secretary` watches a user's **own authorised** mail, calendar
and tasks plus optional public sources, decides conservatively whether anything
merits interrupting them, and delivers at most a short message through the
native Hermes scheduler. It also holds explicit, user-requested reminders
(event-bound or exact-time) in a durable owner-only registry and dispatches
them once.

It is **read-only over the user's sources** and makes no calendar/mail/task
writes, no purchases, and no bookings. The evaluator runs with a resolved
zero-tool toolset, so it can only judge the supplied evidence packet.

## Onboarding and model routes (0.5.1 seam)

- Start or resume with `/secretary setup`; `/secretary status` inspects and
  `/secretary cancel` cancels while retaining progress.
- Use the `secretary_onboarding` tool for every state change and exact readback.
  Foreground memory may be *proposed*; it is never source consent. Ask only
  missing/conflicting/stale facts and genuine permissions. Never ask for
  credentials in chat.
- The native plugin form also has primary/fallback provider+model fields. Pairs
  are both-or-none; assigned primary -> assigned fallback -> stop. A native
  `auxiliary.proactive_secretary` pin is owner configuration and always wins; the
  plugin never rewrites it.
- If `secretary_onboarding inspect` reports
  `configured_model_calls_allowed: false`, model-dependent secretary work is
  unsupported on this host. Do not claim a working fallback ladder; deterministic
  controls and requested reminders still work.
- A destination is recorded as `user_confirmed` at most, never live-verified.
  Confirmation leaves setup at `pending_verification`; do not claim jobs are
  armed or delivery works. A canary needs the user's explicit go and normal host
  tools.
- Private preparation is opt-in and off by default. `preparation_scopes` uses
  exactly conversation/public/mail/calendar/tasks/documents/memory; public
  consent never grants a private scope. Enabling it arms no job. On a host
  without the explicit configured-only contract, `hermes secretary prepare`
  fails closed before any model call; report that limit and do not claim an
  artifact was prepared.
- Never describe the model-outage notice as verified on this host: the generic
  pipeline has no truthful primary/fallback attempt metadata, so no outage
  outcome is fabricated. Requested reminders remain deterministic.

## When to use this skill

Load it when the user wants to:
- set the plugin up for the first time;
- see whether it is running and what it monitors;
- pause, resume, disable or uninstall it;
- record or inspect notification feedback (assistant-mediated corrections);
- refresh or inspect the bounded assistant-mediated context snapshot;
- save or inspect a public watch;
- add or cancel a requested reminder;
- understand why an alert was (or was not) sent.

## First-time agent-assisted installation

Read the repository's `docs/AGENT_SETUP.md` before installing or configuring a new
user's deployment. Discover the active profile and existing connectors, obtain
an explicitly authorised destination, review timezone/quiet hours and public
interests, and prepare with `setup --no-cron` before arming jobs. The repository
is private and requires access.

Write and validate the generic per-user configuration instead of hand-editing:

```bash
hermes secretary configure init        # neutral example (timezone UTC, no accounts)
hermes secretary configure validate    # fail-closed; non-zero on bad/corrupt config
hermes secretary configure preview     # policy keys that would change
hermes secretary configure migrate --from-policy '<policy.json>' --dry-run
```

Name exactly the mail accounts, calendar accounts/IDs and independent task accounts/lists that may be
read; credential presence is never permission. The configuration is versioned,
rejects credential values, and never silently rewrites a nonempty file. Optional
private connectors (productivity-secretary library, Google Workspace calendar)
are off unless explicitly enabled; report missing coverage instead of silently
expanding access.

Source schema: `mail.accounts`/`mail.aliases`, `calendar.accounts`/
`calendar.calendars`, `tasks.accounts`/`tasks.lists`/`tasks.adapter`. New defaults
have empty account arrays and disabled sources/adapters. Empty account arrays
read no connector; empty calendar IDs retain selected/primary/holiday calendars
within configured accounts, and empty task lists retain all visible lists within
configured task accounts. Nonempty IDs/lists restrict actual requests; task list
entries are exact case-sensitive IDs/titles (`id:ID` or `name:TITLE` disambiguate).
Supported task adapters: empty, `tasks`, `productivity_secretary`; other values
fail closed. Tasks use the compatibility library, not the Google Workspace route.

Migration options: repeatable `--mail-account`, `--calendar-account`,
`--calendar-id`, `--task-account`, `--task-list`, plus `--adapter`. Inputs win;
omission preserves prior explicit scope, aliases, interests and adapters,
including empty arrays. Review `report.source_mappings`. Explicit legacy
migration alone may record formerly omitted account discovery in
`advanced.legacy_account_discovery`; old user configs without `tasks.accounts`
copy explicit calendar accounts with a warning. Set task accounts explicitly to
separate them, including task-only monitoring with calendars disabled.

## One-command setup

```bash
hermes plugins enable hermes-proactive-secretary   # once
hermes secretary setup                             # idempotent; safe to re-run
```

`setup` is the whole setup. It:

1. creates the owner-only (`0700`) runtime data directory;
2. if `--import-from <legacy_tree>` is given, copies durable legacy state
   (`var/`, `policy/policy.json`, `policy/preferences.json`) byte-for-byte and
   privately, refusing any conflicting target (use `--import-dry-run` first to
   preview an inventory with counts and hashes and change nothing);
3. writes a policy file from the shipped generic default **if absent** (it never
   overwrites an existing policy without `--force`, and never over an imported
   one);
4. detects which connectors are available on this machine;
5. runs the offline self-test and an offline no-model dry-run **before** arming
   anything;
6. only after those checks pass, installs the launchers and creates the two
   native cron jobs — monitoring every 15 minutes, requested-reminder dispatcher every
   1 minute — verifying each exists;
7. prints a short summary: what is enabled, what is monitored, what is **not**
   covered, and how to pause.

A failing offline check therefore creates **zero** cron jobs. If job creation
fails part-way, setup removes only the jobs it created in that run and reports
any removal it could not complete; it never claims a failed cleanup succeeded.

### Importing existing legacy state

```bash
hermes secretary setup --import-from /path/to/legacy-tree --import-dry-run  # preview only
hermes secretary setup --import-from /path/to/legacy-tree                   # copy, then verify
```

The import never rebuilds reminders, never overwrites an existing file, and
leaves the legacy tree and any legacy cron jobs untouched (the parent pauses and
verifies them separately). A second identical import is a safe no-op. Symlinks
and path traversal are refused; only narrow transient/cache/shadow artifacts are
excluded, and excluded filenames are reported without their contents.

### What to check after setup

```bash
hermes secretary status        # enabled? which jobs? last run? counts?
hermes secretary doctor        # wiring + environment diagnostics
hermes secretary self-test     # full offline deterministic suite
```

Confirm:
- `status` reports `enabled` and lists `hermes-proactive-secretary-monitoring` and
  `hermes-proactive-secretary-reminders` (plus the optional preparation job when recorded);
- `doctor` exits 0 (cron backend available; launchers present);
- `self-test` reports all checks passed.

## Day-to-day commands

| Command | What it does |
|---|---|
| `hermes secretary setup` | One-command, idempotent setup + verification (add `--force` to rewrite policy; `--no-cron` for offline checks) |
| `hermes secretary status` | enabled/disabled, job state, last run, coverage, counts |
| `hermes secretary pause` | Global pause: stops collection **and** delivery |
| `hermes secretary resume` | Clears the global pause |
| `hermes plugins enable hermes-proactive-secretary` | (native) re-enable after a native disable |
| `hermes secretary disable` | Pauses and removes **only this plugin's** two cron jobs |
| `hermes secretary uninstall` | Alias of disable; runtime data is preserved (`--purge` deletes it) |

Every command accepts `--json` and exits non-zero on failure.

### Native toggle vs. secretary disable

The two runners check the native `plugins.enabled` / `plugins.disabled` config
before they do any work. `hermes plugins disable hermes-proactive-secretary`
therefore stops collection and delivery on each **next** invocation and the
runners exit silently — but it does **not** remove the cron schedules (it never
can), so inert schedules remain until you clean them up. Because that toggle is
read from `plugins.enabled`, a missing, malformed or empty enabled list fails
closed for this opt-in plugin, so the runners stay quiet rather than run
unmanaged.

Use `hermes secretary disable` (or `uninstall`) for an explicit pause **plus**
cleanup of this plugin's own jobs and installed launchers before a native
uninstall. Already-delivered messages are not revoked; only future runs stop.
| `hermes secretary self-test` | Offline deterministic self-test |
| `hermes secretary run-once` | Dry, no-model run; never sends (add `--fixture <name>` for an offline run) |
| `hermes secretary watch add <topic>` | Save a public watch |
| `hermes secretary watch list` | List public watches |
| `hermes secretary watch remove <id>` | Remove a public watch |
| `hermes secretary feedback <action> [options]` | Record/inspect preferences (see below) |
| `hermes secretary configure <show\|validate\|preview\|init\|migrate>` | Validated per-user configuration (accounts, calendars, sources, bounds) |
| `hermes secretary doctor` | Environment and wiring diagnostics |

Every command accepts `--json` and exits non-zero on failure.

## Feedback

Feedback is **assistant-mediated, never conversation surveillance**. The
assistant listens to the user's explicit correction in the current conversation,
translates it into a typed command, writes it, reads it back and shows a receipt.
There is no automatic capture hook and no monitoring of unrelated conversations.

Assistant-mediated correction workflow (do all four steps):

1. **Interpret** the correction. If it is ambiguous, ask one clarifying question
   — do not guess and do not write anything yet.
2. **Apply** the narrowest typed command that matches the intent.
3. **Read back** the exact affected record (`show --id`, or the `receipt` field
   returned by the write) and confirm it matches the user's words.
4. **Receipt**: tell the user the preference id, scope and (for a snooze)
   expiry, or that it was a no-op/needs clarification.

```bash
hermes secretary feedback add-explicit --kind mail --match newsletter \
  --scope newsletters --provenance "user asked to stop newsletters"
hermes secretary feedback add-explicit --kind mail --match career \
  --scope "generic career broadcasts" --provenance "user correction" \
  --conditional --exception "unusually fitting"
hermes secretary feedback add-explicit --kind feed --match "formula one" \
  --scope "F1 only" --provenance "user correction" \
  --expires-at <timezone-aware ISO>          # time-boxed durable rule
hermes secretary feedback add-inferred --kind mail --match career \
  --note "tentative pattern" --confidence low --provenance "observed replies"
hermes secretary feedback snooze --kind mail --match career --scope "today only" \
  --provenance "user said not today" --minutes 240
hermes secretary feedback pause --provenance "user asked"
hermes secretary feedback resume --provenance "user asked"
hermes secretary feedback list
hermes secretary feedback show --id <exact existing id>     # typed read-back
hermes secretary feedback forget --id <exact existing id>  # erase record (no tombstone)
hermes secretary feedback noop                             # thanks/silence: writes nothing
```

Rules:
- A clear correction is durable without asking again; inferred patterns stay
  advisory and revisable. Conditional rules keep their exception (career updates
  *unless unusually fitting* is not "never career updates").
- "Not today" is a scoped, temporary snooze — never a permanent ban. Keep the
  expiry; never widen a snooze into a suppression.
- A plain "thanks", acknowledgement or silence is a **no-op** (`noop`, or simply
  say nothing): it is never recorded as a preference.
- Read back the exact affected preference after a write before claiming saved.
- Bad input and corrupt artifacts fail closed (non-zero, nothing written). An
  explicit `forget` erases that stored preference record (after typed read-back);
  it does NOT create a topic tombstone. To prevent an inferred topic from being
  rediscovered, use `inferred-revoke`, `watch-forget`, or `context-revoke`, which
  are the durable revocation paths. A user may explicitly re-add a forgotten
  preference later; that is not a revival of hidden state.

## Assistant-mediated context snapshot

The assistant may hand the scheduled pipeline a **bounded, expiring** set of
context entries it already knows from authorised memory/knowledge. This is a
delivery cache, *not* a second memory backend: the pipeline never scrapes
USER/MEMORY/Hindsight/Library, credentials, connectors or new allowlists itself.

```bash
hermes secretary feedback context-list                     # metadata only (no text)
hermes secretary feedback context-show --id <entry id>     # explicit content read
hermes secretary feedback context-update --note "planning a Japan trip in spring" \
  --provenance "assistant:memory" --kind interest --expires-at <timezone-aware ISO>
hermes secretary feedback context-update --note "prefers no calls before 9am" \
  --provenance "user-stated" --kind preference --owner-declared
hermes secretary feedback context-revoke --id <entry id>   # durable revoke
```

Rules:
- Refresh the snapshot only when the user's relevant knowledge changes (setup or
  an explicit session correction). Send a few bounded entries; never dump an
  entire vault or guess consent. The existing native services stay authoritative.
- Each entry carries a stable id, a provenance label, a timestamp/expiry and an
  advisory-vs-owner-declared flag. `--provenance` must be one of the fixed
  low-sensitivity labels `user-stated`, `assistant:memory`,
  `assistant:knowledge` (default), `owner-config` or `synthetic-test`; an
  arbitrary owner/account/path/request string is refused at write and read time
  so metadata stays generic and portable. Expiry is **mandatory and bounded**: if
  `--expires-at` is omitted the writer applies a 7-day default, and no entry may
  outlive 30 days measured from `updated_at`. A stored entry with a
  missing/malformed expiry, an unknown kind, a malformed id, a non-enum
  provenance, an impossible timestamp order
  (`created_at <= updated_at <= expires_at`), a far-future `updated_at`, or
  injection/secret text makes the whole snapshot invalid at read time (fail
  closed). Timestamps are normalised to UTC-aware (a naive stored value is read
  as UTC, never crashed on). Default is empty; expired valid entries are ignored,
  and invalid/missing entries are simply unavailable and are never fabricated or
  refreshed in the background.
- The snapshot is treated as **untrusted advisory evidence** by the evaluator
  and proposer. It can never change permissions, settings or gates, and copied
  identifiers/names/account details are structurally screened by the same
  privacy boundary — a conservative fail-closed screen, not a proof that no
  possible identifier can ever escape. Raw context is never written to
  status/diagnostic logs.

## Requested reminders

Use the durable registry, never an untracked time-only cron job. The registered
`hermes secretary reminders` handler is deterministic (zero model calls) and
runs with the same validated configuration precedence as monitoring:

```bash
hermes secretary reminders list          # inspect the registry
hermes secretary reminders status
hermes secretary reminders dispatch      # scheduled path (one bounded stdout line)
```

Event-bound and time-only reminders are created by the underlying registry
command with `--add-time`/`--add-event` (public watches use the separate
`watch` command). Prefer: ask for the exact event, the authorised alias /
calendar ID / event ID, timezone and requested lead time; only then register.
Inspect with the registry list/status and cancel by exact id. Never fabricate
provenance or bind to an ambiguous message. Event reads are authorised ONLY by
the explicit `calendar.accounts`/`calendar.calendars`; mail accounts are never
unioned in, a disabled or empty calendar scope reads no event source, and the
optional Google Workspace adapter reports event fresh-get as explicitly
unsupported (non-destructive).

Requested reminders bypass ordinary quiet hours / daily cap / cooldown (the user
explicitly asked) but **never** a global pause and never the explicit-provenance
requirement. Delivery is best-effort; do not promise hard punctuality.

## Honest coverage limits

- The collectors read the user's own **authorised** mail/calendar/tasks. A user
  with no such connector gets **public-source monitoring only** — say so.
- Undated tasks are not covered.
- Blocked, failed, deferred and truncated sources are recorded separately; do
  not promise complete coverage.
- **Source health is limited repair, not self-healing.** Ordinary scans do not
  spam a coverage notice. The plugin keeps a per-source incident (attempted
  checks, failure streak, timestamps), retries boundedly, and honours a `429`
  `Retry-After` with a real per-source dispatch gate. It alerts **once per
  unresolved incident** only for a source the user explicitly named critical
  (`health.critical_sources`; mail/calendar are not critical by default) that
   failed repeatedly for the persistent window (>= 2h) after safe recovery was
   exhausted AND a narrowly classified authorization/grant problem requires
   owner action. Provider overload and unexplained connectivity loss stay
   internal. Two next-scheduled probes are bounded by existing quotas; later
   cooldown-spaced reads are ordinary checks. It cannot refresh revoked grants
   or edit authorization/configuration. Incident-specific emission is persisted
   as pending before stdout; interrupted emission is uncertain across restart
   and is never blindly resent.
- Public feed items are tentative public evidence, not obligations. A forecast
  is a forecast, not an active warning.
- A 200 JavaScript shell is not content; the optional v0.4.0 render fallback and
  the Firecrawl route are **off by default**, consent-gated, and reported candidly
  by `status`/`doctor`. A challenge/interstitial/paywall page is terminal and is
  never rendered or bypassed. Firecrawl runs only through the host-owned
  `ctx.call_mcp` connection and a named server/tool; every call is a locally
  capped, persisted attempt (per cycle and per UTC day, with a 429 cooldown), and
  local attempts are not provider billing credits. Never enable a paid/consented
  route on the user's behalf, never supply a key, and never fabricate fallback
  success.
- The evaluator is conservative: silence is the default and always acceptable.
- "Emitted" means the plugin wrote a message; **actual delivery is owned by the
  native scheduler** and is not confirmed by the plugin.

## Troubleshooting

- **`setup` fails with "cron surface unavailable"** — the Hermes cron CLI is not
  reachable. Confirm `hermes cron` works, then re-run `hermes secretary setup`.
  Use `--no-cron` only for an offline check; it does not create the jobs.
- **`doctor` fails a critical check** — read the printed check name. Missing
  launchers/policy mean the plugin tree is incomplete; a missing cron backend
  means the scheduler is unavailable.
- **No alerts** — first check `hermes secretary status` (paused? jobs present?),
  then `hermes secretary run-once --check` for a coverage diagnostic. An empty
  result is not proof of healthy collection.
- **Nothing is urgent** — silence is expected. The evaluator only notifies for
  genuine, evidence-backed, time-bound items.
- **A reminder did not fire** — inspect the registry list/status. Overlap makes
  a dispatch skip silently by design; `emitted` is not delivery-confirmed.
- **State or preferences look corrupt** — the runtime fails closed rather than
  re-sending. Restore from a known-good file; do not delete the lock or reset
  dedupe state to "fix" a symptom.
- **Pause did not take** — `hermes secretary pause` is global and stops
  collection and delivery including urgency. `resume` is the only clear.

## Verification (for the agent)

Before claiming success, do all of:
1. `hermes secretary setup` exits 0 and lists both jobs.
2. `hermes secretary status` shows enabled + both jobs + a monitoring summary.
3. `hermes secretary self-test` reports all checks passed.
4. `hermes secretary doctor` exits 0.
5. State the coverage limits above rather than equating passing synthetic tests
   with secretary-quality judgment.
