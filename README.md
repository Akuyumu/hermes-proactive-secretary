# Hermes Proactive Secretary

A plugin for an existing Hermes profile. It reads the sources you authorise, sends a short notice when something needs you, and delivers reminders you asked for. An opt-in adds a scheduled copy of your ordinary agent that investigates, keeps follow-ups and stages drafts. Same profile, tools, memory and permissions.

Version 1.2.1, for Linux or macOS with `hermes cron`.

```bash
hermes plugins install Akuyumu/hermes-proactive-secretary --no-enable
hermes plugins enable hermes-proactive-secretary
hermes secretary doctor
```

## What this feels like

Ordinary notices arrive one at a time. Each passes quiet hours, a minimum gap between messages, duplicate suppression, your optional daily ceiling and its own expiry.

Answer a notice the way you would answer a person: `done` when handled, `stop` to stop that item, `later 30` to snooze it. Send the answer as your whole reply; the agent supplies the words that identify which notice you mean. A reply the tool cannot read that way is refused.

## What you get

**A notice lane.** Monitoring collects from enabled sources, keeps what changed, and gates each candidate. `hermes secretary status` reports checks, skips and coverage gaps.

**Requested reminders.** "Remind me on Thursday about the parcel" is stored and delivered through your normal Hermes channel. No model calls, so it keeps working while ordinary notices are quiet or capped.

**An optional background agent.** With `--with-portal`, a scheduled Hermes session keeps follow-ups with an expiry, investigates, proposes one bounded notice through the gates, and stages drafts. `--with-preparation` adds a script job for briefings and draft skeletons; it is off until you enable it and choose scopes.

**Your corrections.** Closing a topic stores a closure until a date or until you reopen it. Context notes are advisory, expire, and never grant access.

## How it works

Four jobs exist; `setup` arms the first two, and the other two are opt-ins.

1. **Monitoring**, every 15 minutes: collection, the gates, delivery. Judgment may use your configured model route.
2. **Reminders**, every minute: a `--no-agent` script job; no model.
3. **The portal** (`--with-portal`): an ordinary Hermes session, created at the 3-hour default cadence. `hermes secretary cadence apply` converts it to a 15-minute tick; the gate then wakes the agent only after your interval, so the tick is a check, not work every 15 minutes.
4. **Preparation** (`--with-preparation`): a script job every 10 minutes, silent unless an artifact is queued.

## Requirements

- A Linux or macOS host with plugin support and a working `hermes cron`.
- Private sources need their accounts connected and scoped separately. The plugin never connects one.
- Model-dependent lanes — monitoring judgment and private preparation — need the host's strict auxiliary task contract, not just a working chat model. `hermes secretary status` reports it as `configured_model_calls_allowed`; when false those lanes stay off instead of borrowing your chat model. Reminders need no model.

## Install

The two plugin commands install it; `doctor` checks the result. Pin a reviewed release with `--ref <commit>`. Host compatibility and upgrades: [installation guide](docs/INSTALLATION.md).

## First run

Choosing what it may read happens in chat. On a fresh profile, send any message in a one-to-one chat and the interview starts; `/secretary setup` resumes it later. It asks for timezone, one delivery route, daily ceiling, quiet hours, autonomy and sources, and arms nothing.

Then arm the jobs:

```bash
hermes secretary setup --no-cron --deliver local  # offline checks, no jobs
hermes secretary setup --deliver local  # arm the two default jobs, output local
hermes secretary setup --deliver telegram:YOUR_CHAT_ID  # arm with a destination you can see
```

Add `--with-portal` and `--with-preparation` when you want them. Start with `local` to see what it would have sent. `setup` reports the destination as unverified; confirm it with one test notice.

## Everyday controls

In chat, the main `/secretary` commands are `setup`, `status`, `pause`, `resume` and `disable`. `pause` stops collection and delivery, reminders included; `resume` reports whether owned jobs are recorded; `disable` pauses and removes only plugin-owned jobs.

From the terminal:

| Command | What it does |
|---|---|
| `hermes secretary status` | Enabled state, jobs, last run, coverage, counts |
| `hermes secretary doctor` | Check the install and configuration |
| `hermes secretary deliveries --since 2026-11-24` | Notices handed to Hermes recently |
| `hermes secretary reminders list` | Reminders you asked for |

A global pause is absolute: reminders stay registered but are not read or sent until you resume, then may fire late. Resume does not recreate removed jobs.

## The tools your agent gets

Eight tools, each with one narrow job:

| Tool | Purpose |
|---|---|
| `secretary_onboarding` | Inspect and confirm setup |
| `secretary_context` | List, update or revoke context notes |
| `secretary_responsibility` | Manage an accepted commitment |
| `secretary_propose_notice` | Propose one bounded notice |
| `secretary_followup` | Create, inspect or stop follow-ups |
| `secretary_draft` | Stage a draft for approval |
| `secretary_guidance` | Close or reopen a topic |
| `secretary_acknowledge` | Record `done`, `later` or `stop` |

`secretary_acknowledge` writes only from an attested foreground turn of your profile; it refuses scheduled runs, delegated children and replayed transcripts.

## Settings

Settings live in the Desktop plugin page and in validated configuration (`hermes secretary configure show`): delivery, sources, cadence and model route, and preparation scopes. Sources are off by default, and an empty public source list means no public coverage. Invalid values are rejected; an explicit empty value stays distinct from an unset one.

## What it will not do

- Its collectors read only sources you enabled and scoped.
- Approving a draft never executes it. A draft records intent; approval records your decision; nothing is sent, booked or changed. The scheduled agent keeps your profile's ordinary tools and permissions, so a draft is a record, not a sandbox.
- It does not create a second memory, permission system or approval path.
- It does not claim coverage it did not achieve.

## Limits worth knowing

- **The delivery ledger is not a receipt.** A row means a notice was handed to Hermes; rows stay `emitted-unconfirmed`. Confirm a destination with a test message through your usual channel.
- **Local-only is not offline.** No external notification is sent, but enabled public checks still contact sites, and judgment uses the provider you configured.
- **Closure matches labels or keys**, so a reworded paraphrase may still arrive.

## Data and upgrades

Runtime state lives in the profile's `plugin-data/hermes-proactive-secretary/`: configuration, preferences, reminders, follow-ups, ledgers and drafts. `disable` preserves it. Upgrading from 1.1.x needs no migration; existing stores and stored jobs are reused.

## Uninstall and reinstall

```bash
hermes secretary uninstall
hermes plugins remove hermes-proactive-secretary
```

`uninstall` pauses collection and delivery, removes the jobs it has recorded (monitoring and reminders, plus the portal and preparation jobs if present) and its runner scripts. `plugins remove` deletes only the code; run it second, because a missing plugin cannot remove its cron entries.

Data survives unless you pass `--purge` to `uninstall`. Reinstall with `--ref <commit>`, run `setup`, and check status: a preserved pause survives, so resume deliberately. Reinstalling does not re-verify delivery; `setup` reports the target as unverified, so confirm with one test notice.

## Check that it works

Run `hermes secretary doctor`, then `hermes secretary status` for jobs, last run, coverage and `configured_model_calls_allowed`. `hermes secretary self-test` runs offline checks; `hermes secretary run-once --fixture healthy_empty` proves only that nothing was emitted. `hermes secretary deliveries --since <timestamp>` lists what was handed to Hermes. Send one test notice to the destination you intend and confirm it arrives; that, not the ledger, is proof of delivery.
