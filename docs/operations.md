# Operations guide

Detailed setup, configuration, controls and troubleshooting for Hermes
Proactive Portal. For the product overview, features and quick install, see
the [README](../README.md). For step-by-step human setup and upgrades, see
[Installation](INSTALLATION.md). For per-version change history and
qualification totals, see
[GitHub Releases](https://github.com/Akuyumu/hermes-proactive-portal/releases).

## First run: prepare, preview, consent

Prepare the owner-only runtime and run offline checks **without arming jobs**:

```bash
hermes secretary setup --no-cron
hermes secretary configure show
hermes secretary configure validate
hermes secretary configure preview
hermes secretary configure project
```

`setup --no-cron` writes a neutral policy/preferences baseline and, on a new install, a neutral `user_config.json`. It creates no cron jobs. `configure preview` shows which policy keys would change. A nonempty configuration is never overwritten without `--force`.

`configure project` is the explicit, idempotent repair for an older deployment whose native plugin form is empty or stale: it validates the *effective* configuration (native settings first, then the local file) and copies only the ordinary form fields into the native form. It never writes or reseeds `user_config.json`, never replaces accounts/scopes/custom source URLs/interests, preserves an explicit empty list, and performs no write or backup when the form already matches. An explicit `setup` (including the existing-policy "kept" path) performs the same safe backfill. Ordinary loads never mutate configuration. Before any migration or projection that would change settings, the plugin writes an owner-only, hash-verified backup of its own `user_config.json`, policy copy, preferences, declared native settings and onboarding record; if that backup cannot be written, no settings write occurs.

Settings, grants and memory facts are separate. Settings are the validated configuration and native form fields. Grants are explicit typed permissions (`mail`, `calendar`, `tasks`, `public_sources`, `memory`, `discretionary`). Foreground-memory facts may be proposed as tentative facts; a proposal is not a fact, and accepting a fact is not a grant.

## Custom settings page and confirmed reset

The unified package ships a custom Settings ▸ Plugins page (`desktop/plugin.js`) backed by fixed, host-authenticated routes in `dashboard/plugin_api.py`. It shows the setup status, humanised source status (Off, Not configured, Unavailable, Not verified, Stale, Fresh), and grouped ordinary controls for setup, notification preferences, sources and advanced bounds. Editing uses the same validated `userconfig` owner as the native Agent settings form and onboarding: values are read from the effective configuration, a save is explicit and foreground-only, unknown keys are rejected, an optimistic revision check refuses stale writes, and the page reads values back after writing. Permission-widening changes are returned as a prospective list that must be reviewed and explicitly confirmed before the write; the server refuses the save without that acknowledgement. There is no second settings store, no arbitrary config path, model or secret write, and no raw JSON as the primary basic experience.

“Trigger onboarding” remains a repair/review door: it only places `/secretary setup` in the live composer. A fresh profile does not need it, because the first eligible conversation starts the interview itself. “Reset and onboard” is a two-step, typed confirmation. A reset clears onboarding progress and everyday preferences and turns reading permissions off; it preserves account connections, custom public source definitions, model routes, reminders/jobs and unrelated runtime state. A private owner-only backup with a SHA-256 manifest is written before any reset write. If a reset is needed, the backup makes the previous settings recoverable by the owner; deleting or losing that backup is what makes the change irrecoverable. The page does not offer a restore button. A read-only status/trigger request never resets settings or arms jobs.

The custom page and backend appear only after the package is installed in the active profile’s `plugins/` root, `hermes-proactive-secretary` is added to `plugins.enabled`, the desktop half is enabled in Capabilities ▸ Plugins, and the gateway is restarted (backend routes mount at startup). Reload or rescan desktop plugins so the app copies and loads the `desktop/` half. These are host/app activation steps and are not performed by packaging.

### Missing the Proactive Portal settings page

Install and enable the backend with the native command:

```bash
hermes plugins install Akuyumu/hermes-proactive-portal --no-enable
hermes plugins enable hermes-proactive-secretary
hermes secretary doctor
```

For the Desktop component, use **Capabilities → Plugins → Install from Git** or the documented unified link:

```text
hermes://plugin/install?repo=Akuyumu/hermes-proactive-portal&enable=1
```

Current Hermes documentation describes one confirmation dialog that detects both components and lets the user choose them. Older Desktop builds can differ; use the controls your installed version exposes rather than assume a component checkbox exists. A private-repository download requires Git access on the machine doing the download; backend access alone does not prove client access. Do not use internal marker files, bypass scanners or weaken checks. See the [official installation and Desktop-link guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins#one-click-install-links-desktop).

The conversational setup interview works without the Desktop half. If the button
is absent, setup is still available from any eligible one-to-one chat; the page
is an optional review/edit surface, not a prerequisite for onboarding.

On a phone, prefer the Telegram DM workflow below. The CLI `setup` command is not a substitute for the conversational onboarding interview.

## Mobile-first Telegram DM workflow

Use a one-to-one DM with the bot. Onboarding deliberately ignores groups, channels, shared chats, cron, a2a, subagent and internal turns.

1. Send any ordinary message in a one-to-one DM. On a fresh profile the interview starts there; no settings edit or command is required. `/secretary setup` remains a fallback that also shows a short-lived continuation code such as `sec-cont-ab12cd34ef56`.
2. Answer one natural question at a time: timezone, one messaging route, daily ceiling and quiet hours, autonomy, private-preparation scopes, then explicit source grants.
3. The autonomy choice is plain language: review only, prepare private reversible drafts, or prepare plus a quick ask before consequential work. In every mode an external send, publication, purchase, booking or calendar/task mutation needs one exact approval naming its opaque reference.
4. If a slash command is used where the host exposes no session id, reply with the answer **and** the code together. The code is a routing token, not a credential.
5. Use `/secretary status` to inspect progress, jobs, the effective model route and limits.

Supported chat controls:

| Command | Effect |
|---|---|
| `/secretary setup` | Start or resume gap-only onboarding |
| `/secretary status` | Inspect onboarding, jobs, route and limits |
| `/secretary pause` | Global pause: stop collection and delivery, including requested reminders |
| `/secretary resume` | Clear the pause; reports whether owned jobs are recorded |
| `/secretary disable` | Pause and remove only plugin-owned jobs/runners |
| `/secretary cancel-setup` | Cancel setup while retaining progress |
| `/secretary cancel` | Alias for cancel-setup; it does not pause alerts |

A global pause is absolute. Requested reminders remain registered but are not read or emitted until you resume; they may then fire late.

Onboarding is resumable and cancellable. The destination is recorded as `user_confirmed` at most, never live-verified. Completion therefore stays `pending_verification`, and **no jobs are armed by onboarding**. The completion text names the exact next command.

The plugin does not promise perfect quote/reply correlation. It uses explicit opaque `sec-...` references when present and asks for clarification when a reference is missing or ambiguous. It never guesses a sender, creates a DM, or runs a global interceptor.

## Arm the owned jobs

Configuration or onboarding alone does not arm anything or deliver anything. After reviewing the preview, arm explicitly:

```bash
hermes secretary setup --deliver local   # safe local/no-send first
hermes secretary setup --deliver telegram
hermes secretary status --json
hermes secretary doctor
hermes secretary self-test
hermes cron list
```

An existing profile that recorded the old `hermes-proactive-secretary-pilot` job can rename it in place, preserving its id, schedule, delivery, enabled/paused state, next run, run history and unrelated fields:

```bash
hermes secretary migrate-jobs          # dry-run: show the exact recorded plan
hermes secretary migrate-jobs --apply  # idempotent in-place rename via native update_job
```

The migration refuses a name-only/foreign row, a mismatched ownership id, or an occupied monitoring name. If the host has no safe native update API it reports a blocker and recreates nothing. Run it before `setup`; setup refuses to create a duplicate while a recorded legacy job is still live.

`telegram` is the native Telegram delivery target. Use `telegram:<chat_id>` only when you already know the exact native chat id and your host's `hermes cron create --help` shows that form. Never guess a chat id. Use `origin` only when the command truly runs with chat context; a terminal command does not.

A successful `setup` creates and records ownership of two native `--no-agent` script jobs: `hermes-proactive-secretary-monitoring` (every 15 minutes; may use a host-owned model lane for judgment) and `hermes-proactive-secretary-reminders` (every minute; deterministic, zero model calls). An explicit `--with-preparation` adds the optional `hermes-proactive-secretary-preparation` job; it is never hidden from status or setup summaries once recorded.

Keep these states distinct: **installed** (plugin present), **configured** (settings validate), **scheduled** (owned jobs recorded), **delivery-confirmed** (a real receipt from an explicit native canary; not a state this plugin can set). This plugin has no send or canary command; a canary is your own foreground action through your existing native channel. If you cannot verify a destination, keep `--deliver local`; the honest state is `unverified`.

## Settings, model routes and attention

The ordinary settings surface is the **native plugin form** (Settings -> Plugins -> Proactive Secretary), projected from the `config_schema` in `plugin.yaml`. It includes timezone, destination, daily ceiling, quiet hours, cooldown, source toggles, public interests/sources, discovery, budgets, preparation and the four model-route fields. The validated `user_config` remains the single owner; the form is not a second store.

Configuration precedence: native `plugins.entries.hermes-proactive-secretary.settings.user_config` and ordinary flat form fields, then local `user_config.json` beside `policy.json`, then neutral defaults. A present-but-malformed native setting fails closed; it never silently falls back. See [`policy/user-config.example.json`](../policy/user-config.example.json) for the neutral shape. The plugin form and onboarding write the same validated owner, so a later form edit and a resumed onboarding review converge on the current effective values rather than an old snapshot.

Model routing: operator native `auxiliary.proactive_secretary` pin **>** plugin primary/fallback fields **>** initial chat-model inheritance only when no secretary primary is configured. `primary_provider`/`primary_model` are both-or-none; `fallback_provider`/`fallback_model` are both-or-none and require an assigned primary. After the assigned fallback fails there is no extra main/global/chat escalation; on a host without the strict contract, nothing is dispatched. Route changes are read by the next secretary process or a native plugin reload; instant in-process reload is not promised.

The **attention envelope**:

| Surface | Counts against the ordinary daily ceiling? | Model? |
|---|---|---|
| Requested reminder (exact-time or event-bound) | No; distinct deterministic policy | No |
| Ordinary alert, discovery or approval request | Yes; shared ceiling, cooldown and dedupe | Yes, when supported |
| Artifact-ready private-preparation notice | Yes; same ceiling/cooldown/dedupe | No extra model call |
| Model-outage notice | Yes; subject to pause/quiet/attention | Never |

The default is **5 per local day**, configurable to **any nonnegative integer**. It is a ceiling, never a target; zero useful reasons means zero messages. Zero suppresses ordinary discretionary messages only, not collection, requested reminders or a genuine operational notice. Urgent evidence uses the existing urgency policy. Quiet hours default to 00:00-08:00; start equal to end gives a zero-length window. Requested reminders bypass quiet hours, cap and cooldown, but never a global pause or the explicit-provenance requirement.

## What it monitors, and what it does not

With explicitly configured and authorised connectors it can read mail metadata through Himalaya for exactly the accounts in `mail.accounts` (never bodies beyond a short redacted preview), calendar/tasks through the external productivity-secretary library or calendar through the optional Google Workspace adapter, and public feeds/pages you explicitly save from public keyless endpoints.

With no private connector it is public-source monitoring only. It does not send mail, mutate calendars or tasks, book, purchase, publish, or perform any external action; guarantee complete coverage (blocked, failed, deferred and truncated sources are recorded separately); cover undated tasks; confirm delivery; or fabricate urgency, personal fit, location or a deadline.

## Onboarding, memory and consent

Conversational onboarding is the primary setup path. The first eligible foreground turn asks the next genuine question, and later turns in that same conversation resume it without a command. Existing configuration and memory are evidence for suggestions, never invented grants. A completed or cancelled interview is not silently reopened; the first-use invitation is emitted at most once per profile. See [agentic onboarding design](agentic-onboarding-design.md) for the product contract.

Onboarding asks only for missing or conflicting facts. It can seed known values from an existing effective configuration, but does not treat generated neutral defaults as user decisions. Foreground memory may be proposed through the `secretary_onboarding` tool. This is deliberately **not** a new memory-provider API and not a background scrape. For context, the scheduled pipeline never scrapes USER/MEMORY/Hindsight/Library, credentials or connectors, and never creates a new allowlist. Conflicting facts require explicit resolution.

Proposals grant no source access; only an explicit typed permission decision does. `resume` reports actual readiness and never claims monitoring is active when no owned job is recorded.

## Requested reminders

Requested reminders live in a durable owner-only registry and dispatch once, deterministically and without a model:

```bash
hermes secretary reminders list
hermes secretary reminders status
```

Event reminders use typed IDs and only the explicit `calendar.accounts`/`calendar.calendars` boundary; mail accounts are never unioned into calendar authorisation. A disabled or empty calendar scope reads no event source while an exact-time reminder still works. `emitted` is not delivery-confirmed, and hard punctuality is not promised.

## Private preparation (opt-in, off by default)

Preparation is off by default. `preparation.enabled` alone arms nothing, reads nothing and grants no scope. When explicitly enabled **and** the host has a verified configured-only model lane: deterministic gates check configuration, enablement, pause, scope and readiness; at most one due **accepted** responsibility is atomically claimed; one bounded structured draft is produced; the deterministic writer checks citations, safe content and SHA-256, then writes one owner-only Markdown/text artifact; a later pipeline run may emit one fixed, opaque "draft is ready" notice through the ordinary attention path.

To install the owned preparation runner and its one native `--no-agent` script job, opt in explicitly:

```bash
hermes secretary setup --with-preparation --deliver local
```

The scope vocabulary is exactly `conversation`, `public`, `mail`, `calendar`, `tasks`, `documents`, `memory`. Broad `public` consent never implies a private scope. A source change, cancellation, pause or scope revocation invalidates stale work before commit. Artifacts are private drafts, not user documents.

Handoff never executes. `hermes secretary responsibilities handoff --id <opaque-id>` prints the current generation, hash and target for ordinary foreground approval; any send, book, purchase, publish or calendar/task mutation still requires your exact approval through normal Hermes tools. On a host without the strict contract, `hermes secretary prepare` reports the limitation and leaves the unit unclaimed.

## Public discovery and privacy

Public discovery is off by default. Add only public, keyless sources you trust, for example through `hermes secretary watch add "<public topic>"` or explicit `public_sources` entries. Supported parser kinds include `github_releases`, `reddit_rss`, `rss_atom`, `web_page`, `nea_forecast`, `ntu_json`, `ntu_opportunity`, `smrt_notice` and `cna_rss`.

When adaptive discovery is explicitly enabled, its query boundary is public and bounded. Queries come from explicit public interests/watches, or tentative inferred goals only if you separately enable them; private facts are never turned into public queries, and a broad public grant never authorises a private source or scope. Discovered content is untrusted evidence, never instructions. Enabling a source does not grant permission to read a different source.

## Adaptive public maintenance and proposals

Status/doctor reports per-public-source truth rather than a single newest success. Cached-but-not-due, budget-deferred, transient/rate-limited and intentional LTA transport-gate skips are not reported as global failure; an unsupported adaptive route is an explicit unresolved limitation.

Within standing public-maintenance consent **and** enabled broad public discovery, a persistently defective **public** source may be replaced automatically only when the existing bounded discovery path finds a candidate, a real fetch yields fresh usable evidence, and a strict zero-tool structured decision verifies the same purpose, authority and date scope. A same-topic search hit is not an equivalent; official SMRT/NTU/weather sources cannot be replaced by forum or project chatter. Missing strict decision, privacy rejection, transient/rate-limit/deferred observations, or disabled/revoked/paused/snoozed sources cause no replacement. The old source is parked with preserved failure history and stable lineage, its evidence/cache is preserved on the adopted source, one concise notice is emitted at most once per change through the ordinary envelope, and `hermes secretary feedback --feedback source-dispute --id <stable-ref>` performs a locked idempotent restore that blocks the same rejected replacement from returning.

Feedback also changes subsequent adaptive acquisition and relevance: explicit corrections revise the plan, explicit suppression or a scoped temporary snooze blocks the watch/evidence without retiring it, and the snooze expires automatically. "Thanks" or silence is not disinterest. Conditional rules are not blanket suppressions.

The plugin may propose a specific typed configuration adjustment from bounded operational evidence (with reason and expiry) through the same five-per-day attention envelope. A proposal never changes configuration; it stays unapplied until exact foreground approval through the existing validated writer/backup path. See [adaptive maintenance](adaptive-maintenance.md).

## Optional integrations

These are optional and off by default; they do not ship with credentials.

- **Himalaya mail**: install and authorise the CLI, then name accounts in `mail.accounts`. Credential presence is not permission.
- **Productivity-secretary library**: point `SECRETARY_SRC` or `PRODUCTIVITY_SECRETARY_SRC` at an existing authorised installation. Calendar/tasks are read-only.
- **Google Workspace calendar**: enable `adapters.gws_calendar` only after the installed `productivity/google-workspace` skill and its existing authorisation are confirmed. The adapter is calendar-read only; there is no verified Google Tasks command and no invented Tasks support.
- **LTA/regional public feeds**: generic examples of supported public-source kinds, not defaults and not tied to any user's location.

See the connector limits and configuration sections in [AGENT_SETUP.md](AGENT_SETUP.md) before enabling a private connector.

## Data, privacy and paths

Runtime state is owner-only (`0700` directories, `0600` files) under the active profile's `plugin-data/hermes-proactive-secretary/`. It contains the policy copy, preferences, user configuration, durable reminders, locks, scheduler/runner ownership ledgers and preparation artifacts, and is never written into the installed plugin tree. One shared owner-path helper resolves the host's context-local active profile for every foreground and preparation owner: runtime data/state/policy/preferences/config, scheduler ledgers and script locations, the enabled gate, and source/connector/token locations. A routed context therefore uses its own profile's home rather than the process launch profile. `HERMES_PROACTIVE_SECRETARY_DATA` is a launch-profile/standalone override only: it is honored when the effective home is the process's own launch profile, while a routed foreign profile ignores it instead of borrowing the launch profile's data. `HERMES_HOME` selects the profile only for standalone use when the host profile API is not installed. If a present host profile getter or launch scope cannot be resolved, every owner fails closed; none falls back to the launch profile or ambient environment. Explicit operator-configured external locations (for example `SECRETARY_SRC`, `HERMES_AGENT_SRC`, an explicit calendar home, or an explicit artifact root) remain explicit and are never silently rewritten under the active profile.

Credentials and OTP messages are never fetched or stored. URLs and codes are redacted from previews. Source content is treated as untrusted evidence. Public retrieval can contact external services; judgment can use the user's configured model provider when the host supports the strict lane. Read-only does not mean offline.

## Migration and uninstall

Preview and import an existing deployment's own durable state; never import another person's deployment:

```bash
hermes secretary setup --import-from '<legacy-tree>' --import-dry-run
hermes secretary setup --import-from '<legacy-tree>'
hermes secretary configure migrate --from-policy '<legacy-policy.json>' --dry-run
hermes secretary configure project
```

Import refuses conflicting targets, rejects symlinks and path traversal, copies owner-only, and never deletes or adopts legacy cron jobs. Migration preserves explicit scope, aliases, interests and adapters; it never moves the global pause into settings.

To stop and remove only this plugin's own jobs and runners while preserving private runtime data:

```bash
hermes secretary disable
hermes plugins disable hermes-proactive-secretary
hermes plugins remove hermes-proactive-secretary
```

`hermes secretary uninstall` is an alias of disable. `--purge` deletes runtime data and requires separate explicit approval; there is no need for a separate manual deletion step. A native `plugins disable` on its own leaves owned schedules present but inert until `hermes secretary disable` removes them. Reinstall may preserve a global pause; review state and resume deliberately.

## Troubleshooting

- **`cron surface unavailable`**: fix `hermes cron` first. `--no-cron` performs offline checks and creates nothing.
- **`doctor` fails**: read the failing check name. Missing launchers or policy mean an incomplete tree; a failing cron check means the scheduler is unavailable.
- **No alerts**: run `hermes secretary status`, then `hermes secretary run-once --check`. An empty result is not proof of healthy collection; silence is the expected default.
- **Reminder did not fire**: inspect `hermes secretary reminders list` and `status`. Overlap skips silently by design; `emitted` is not delivery-confirmed.
- **Pause did not take**: pause is global. Use `hermes secretary resume` to clear it; requested reminders also wait while paused.
- **Model-dependent work says unsupported**: the host lacks the explicit configured-only contract. Deterministic controls and reminders still work; do not claim a fallback ladder.
- **Corrupt state or preferences**: the runtime fails closed. Restore a known-good file; do not delete the lock to "fix" a symptom.

## Development and release

Maintainer commands from the source tree, using a Python environment with the Hermes runtime and test dependencies already available (`python3` below must refer to that environment):

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m proactive_pilot.selftest
python3 scripts/release_export.py scan
python3 scripts/release_export.py build --output ./dist/hermes-proactive-portal-1.0.3.tar.gz
python3 scripts/release_export.py doctor --installed /path/to/extracted-package
```

`scripts/release_export.py` is the repository-only developer implementation. The old root `release_export.py` remains a thin import/CLI compatibility shim for existing commands and tests; prefer the `scripts/` path for new automation. Both the shim and the `scripts/` package are development tooling and are excluded from the exported archive.

`scripts/release_export.py` builds a deterministic code-only archive, excludes tests, runtime state and `.build-context`, verifies the manifest/module version and scans for personal paths. `doctor` validates package-local files, imports the extracted entrypoints and delegates host validation to `hermes plugins doctor --ci` under an isolated profile. These commands validate packaging; they do not constitute live delivery, connector, model or device qualification.

Keep three script locations distinct. Repository scripts such as `scripts/release_export.py` are maintainer-only tooling and never ship. The shipped `runners/*.py` templates are package content. The profile-installed scripts under the active profile's `scripts/` directory, for example `$HERMES_HOME/scripts/proactive_secretary_*.py`, are owned copies that `hermes secretary setup` installs for the native scheduler; the exporter neither installs nor runs them.

## Qualification and limitations

Qualification results are recorded per version in
[GitHub Releases](https://github.com/Akuyumu/hermes-proactive-portal/releases).
They do not establish that a particular user's sources have useful live
alternatives or that a physical device received a notification. Real connector
access, model calls, messaging delivery and Desktop/device interaction require
separate installation-specific qualification.

Source coverage is bounded, quiet/attention behavior is deterministic but not omniscient, and model-dependent features require a compatible host exposing the strict configured auxiliary lane. Validate the exact connectors and messaging route you intend to use. Configuration proposals remain foreground-approved typed handoffs, not automatic settings changes. A defective official source with no verified equivalent remains honestly unresolved. The custom settings page, grouped editable controls and confirmed reset retain their synthetic host/API/Node qualification; no live Desktop visual, accessibility or physical-device result is claimed by the package.

For agent-assisted installation, give your agent [AGENT_SETUP.md](AGENT_SETUP.md). The operational skill is [skills/proactive-secretary/SKILL.md](../skills/proactive-secretary/SKILL.md).

## License

MIT. See [LICENSE](../LICENSE).
