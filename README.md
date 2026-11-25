# Hermes Proactive Secretary

**A little less checking. A little more done.**

This is a Hermes plugin, not a separate assistant. It watches the sources you
have authorised and tells you when something needs you; it runs the reminders
you asked for; and — only if you opt in — it runs a scheduled copy of your normal
agent that can investigate, keep follow-ups and stage drafts for your approval.
It uses your existing profile: same tools, same memory, same permissions.

![How it works: authorised sources feed scheduled checks, which pass notification gates before anything reaches you](docs/assets/how-it-works.svg)

## What you get

### A short notice when something actually needs you

Ordinary notices are checked before they reach you: quiet hours, a minimum gap
between messages, duplicate suppression for the same event, your optional daily
ceiling, and any expiry the notice carries. Urgent evidence and reminders you
asked for have their own exception rather than a free pass.

Collectors are deterministic, and only what changed goes on to judgment.
`hermes secretary status` shows what was checked, what was skipped and where
coverage was missing, so a source that could not be read shows up as a gap
instead of a silent success.

### Reminders you asked for

"Remind me on Thursday about the parcel." Added from chat or the CLI, stored, and
delivered through your normal Hermes channel. The reminder job is deterministic
and makes no model calls, so it keeps working while ordinary notices are quiet or
capped.

### Optional: a background agent that does the thinking

With `hermes secretary setup --with-portal`, a scheduled copy of your Hermes
agent wakes on your cadence and can:

- **Keep follow-ups.** "Keep an eye on the visa page" becomes an inspectable,
  stoppable follow-up with an expiry. You can file one, and so can the agent.
- **Investigate and propose.** It can look into a change and propose one bounded
  notice through the same gates.
- **Stage drafts.** Anything it would send, book, buy or change is written to a
  pending draft with its body and manifest. Approving it in a foreground session
  moves it to approved and records your decision; the plugin itself never
  performs the action.
- **Respect your corrections.** Once you say a topic is finished, it stops
  raising it (see below).

That agent is your ordinary profile agent: it keeps your existing tools and
permissions. The plugin adds no sandbox around it and does not widen what the
profile could already do.

### Optional: private preparation for accepted commitments

With `--with-preparation`, a separate script job prepares material — briefings,
checklists, draft skeletons — for responsibilities you have accepted. It is off
until you enable it and choose what it may use, and it never sends anything.

### It remembers your corrections

- **Topic closure.** When you ask to stop notices about a topic, the tool then
  gives you a one-line description of the exact change — for example
  `close "exams" until 2026-12-02` — and writes only after you reply that it is
  right. Requested reminders are unaffected. A permanent closure needs you to say
  so.
- **Bounded context.** A few short, expiring notes about your projects,
  preferences and people, used to make notices relevant. Advisory only: they
  never grant access or consent.
- **Adaptive maintenance.** With your consent, a public source that keeps failing
  can be replaced by a verified equivalent, and you can undo it. Suggested
  configuration changes stay proposals; nothing is edited silently.

## How it works

Four jobs exist. `hermes secretary setup` arms the first two together; the
other two are separate opt-ins:

1. **Monitoring** — every 15 minutes: deterministic collectors for your enabled
   sources, then the notification gates, then delivery through Hermes. Collection
   and gating are deterministic; judgment about what changed may use the model
   route you configured.
2. **Reminders** — every minute, deterministic and model-free.
3. **The optional agent** (`--with-portal`) — a scheduled ordinary Hermes
   session for investigation, follow-ups and drafts. It uses more resources and
   can be stopped without touching the monitors.
4. **Private preparation** (`--with-preparation`) — a separate script job that
   prepares material for accepted commitments, and exists only when you opt in.

Configuration or onboarding alone arms nothing. Jobs are created explicitly by
`hermes secretary setup`.

## Requirements

- A Linux or macOS Hermes profile with plugin support and working `hermes cron`.
- Private sources (mail, calendar, tasks) need accounts connected and scoped
  separately; the plugin never connects one for you.
- Model-dependent features need a working model route. Reminders do not.

## Install

```bash
hermes plugins install Akuyumu/hermes-proactive-secretary --no-enable
hermes plugins enable hermes-proactive-secretary
hermes secretary doctor
```

Desktop users can install from the native consent link instead:

```text
hermes://plugin/install?repo=Akuyumu/hermes-proactive-secretary&enable=1
```

The [installation guide](docs/INSTALLATION.md) covers compatibility, upgrades and
removal.

## First run

Choosing what it may read happens in **chat**, not in the CLI. On a fresh profile
send any message in a one-to-one chat and the short interview starts;
`/secretary setup` resumes it later. It asks for your timezone, one delivery
route, daily ceiling and quiet hours, how much autonomy you want, and which
sources you grant. The interview arms nothing.

When you are ready, arm the jobs from the terminal:

```bash
hermes secretary setup --no-cron --deliver local   # trial: verify offline, arm nothing
hermes secretary setup --deliver local             # arm monitoring and reminders, record locally
hermes secretary setup --deliver telegram          # arm them with a delivery route
hermes secretary status
```

Add `--with-portal` for the scheduled agent and `--with-preparation` for the
preparation job. Start with `local` delivery to see what it would have sent
before it sends anything.

## Everyday controls

In chat:

| Command | Effect |
|---|---|
| `/secretary setup` | Start or resume the setup interview |
| `/secretary status` | Onboarding, jobs, model route and limits |
| `/secretary pause` | Stop collection and delivery, including requested reminders |
| `/secretary resume` | Clear the pause; reports whether owned jobs are recorded |
| `/secretary disable` | Pause and remove only plugin-owned jobs |

From the terminal:

| Command | What it does |
|---|---|
| `hermes secretary status` | Enabled state, jobs, last run, coverage and counts |
| `hermes secretary doctor` | Check the install and configuration |
| `hermes secretary deliveries --since 2026-11-24` | Notices handed to Hermes recently |
| `hermes secretary reminders list` | Reminders you asked for |
| `hermes secretary watch list` | Public watches |
| `hermes secretary guidance list` | Topics currently muted |
| `hermes secretary cadence set --set 3h` | How often the optional agent wakes (no job change) |
| `hermes secretary cadence apply` | Install the cadence gate once |
| `hermes secretary job status` | The optional agent job's state |
| `hermes secretary disable` | Pause and remove owned jobs; data is preserved |

A global pause is absolute: requested reminders stay registered but are not read
or sent until you resume, and they may then fire late. Resume clears the pause;
it does not recreate jobs that were removed.

## The tools your agent gets

The plugin hands your normal agent seven small tools, each with a narrow job:

| Tool | Purpose |
|---|---|
| `secretary_onboarding` | Inspect, propose, validate and confirm setup |
| `secretary_context` | List, show, update or revoke bounded context entries |
| `secretary_responsibility` | Capture and manage an accepted commitment |
| `secretary_followup` | Create, inspect or stop a follow-up |
| `secretary_guidance` | Close or reopen a topic for the notice lane |
| `secretary_draft` | Stage a draft for owner approval |
| `secretary_propose_notice` | Propose one bounded notice through the gates |

## Settings

Settings live in the Desktop plugin page and in validated configuration
(`hermes secretary configure show`). The ones that matter:

- **Delivery** — destination, quiet hours, cooldown, optional daily ceiling.
- **Sources** — mail, calendar, tasks, public feeds, and a narrower
  "due payments and orders" switch for mail. All off by default; an empty public
  source list means no public coverage.
- **Cadence and route** — how often the optional agent wakes, and which model it
  uses.
- **Preparation** — whether private preparation is allowed, and in which scopes.

Invalid values are rejected, and an explicit empty value is kept distinct from an
unset one.

## What it will not do

- The plugin's own collectors use only the sources you enabled and scoped.
- Its draft path never executes anything: approving a draft records your decision.
- It does not create a second memory, permission system or approval path.
- It does not claim coverage it did not achieve.

The optional background agent is a normal Hermes session. It acts with your
profile's existing tools and permissions, so its safety comes from Hermes' own
approval gates, not from a plugin sandbox.

## Limits worth knowing

- **The delivery ledger is not a receipt.** A row means a notice was handed to
  Hermes, and rows stay `emitted-unconfirmed`. To test a destination, send a test
  message through your usual Hermes channel and confirm you receive it.
- **Local-only is not offline.** It means no external notification; enabled
  public checks can still contact sites, and judgment uses the provider you
  configured.
- **Topic closure trusts the platform, not the chat type.** Hermes reports the
  platform and sender, not whether a chat is a group, so a group message can look
  like a direct one. A recent message can also be reused for up to 30 minutes,
  because the host exposes no message id.
- **Closure matches labels or keys**, so a differently worded paraphrase of the
  same topic may still arrive.

## Data and upgrades

Runtime state lives in the active profile's
`plugin-data/hermes-proactive-secretary/`: configuration, reminders, ledgers and
drafts. `disable` preserves it. Upgrading from the old plugin id copies durable
state once; Desktop settings may need re-entering. See
[operations](docs/operations.md).

## Learn more

- [Installation](docs/INSTALLATION.md)
- [Agent-assisted setup](docs/AGENT_SETUP.md)
- [Operations guide](docs/operations.md)
- [Background agent](docs/NATIVE-PORTAL.md)
- [Notification contract](docs/notification-contract.md)
- [Adaptive maintenance](docs/adaptive-maintenance.md)
- [Engineering notes](docs/ENGINEERING.md)
- [Contributing](CONTRIBUTING.md)

## License

[MIT](LICENSE).
