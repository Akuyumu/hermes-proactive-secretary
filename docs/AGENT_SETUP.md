# Hermes Proactive Secretary: agent-assisted installation and setup

> **Human setup:** start with [INSTALLATION.md](INSTALLATION.md). Host compatibility, the reviewed patch base and safe apply/reverse steps are in [ENGINEERING.md](ENGINEERING.md) and [compatibility/hermes-host-portal.patch](compatibility/hermes-host-portal.patch).

Give this file to your Hermes agent with:

> Install and configure Hermes Proactive Secretary using this guide. Inspect my environment first, ask only for missing choices or authorisation, preserve existing configuration, and verify the result. Do not claim a connector works merely because it is installed.

This guide uses no original owner's state, delivery address, credentials or interests. The repository is public at `Akuyumu/hermes-proactive-portal`; installation uses the public Git URL and needs no access grant beyond ordinary read access. The native plugin id is `hermes-proactive-secretary`. Existing state is copied once from the legacy data root on first load, and old owner-facing jobs are retired through the ownership ledger.

## 1. Inspect before changing anything

- Confirm Hermes is installed and inspect `hermes plugins install --help` and `hermes cron --help`. This package targets Linux/macOS; Windows and fresh-machine portability have not been independently certified.
- Resolve the active profile. Honour `HERMES_HOME`; do not configure a different profile or assume the default home.
- Inspect existing plugins and scheduled jobs. If already installed, use `hermes secretary status` and `doctor` before reinstalling. Do not use `--force`, remove foreign jobs, or overwrite existing state to make setup succeed.
- Check native model availability, configured public-search availability if requested, and the messaging gateway. Do not expose credentials or alter the user's provider, permissions, or paid services.
- Explain that authorised evidence is sent to the configured model for judgment. Public acquisition uses external websites/search; read-only does not mean offline.

## 2. Gather the few choices that cannot safely be guessed

Ask together, only when not already explicitly established:

1. Delivery: which already authorised channel/chat should receive alerts? Verify its exact destination through the existing gateway. Otherwise use `local`, which saves output without messaging anyone.
2. Timezone and quiet hours: the shipped defaults are neutral `UTC`, midnight to 08:00, five ordinary alerts per day and a 30-minute cooldown. These are defaults, not an inferred user location or preference.
3. Coverage: public sources only, or supported, already authorised private connectors too? Ask for public interests/source URLs if the user wants public monitoring.
4. Adaptive discovery: whether to enable model-driven public-interest discovery, with the paired privacy review. It is off in the generic defaults and can add model/search usage.

Optional v0.4.0 routed-retrieval fallbacks (`native_render_fallback_enabled`/`native_render_consent`, `firecrawl_enabled`/`firecrawl_consent`) are **off by default** and must not be turned on speculatively. They only ever apply to a `dynamic` JavaScript shell on a public page; a challenge/interstitial/paywall page is terminal. Firecrawl goes through the supported native `ctx.call_mcp` contract — the host owns the OAuth connection and the operator's MCP allowlist; name an already-configured MCP server/tool (`firecrawl_mcp_server`/`firecrawl_mcp_tool`), never an endpoint or a key, and never sign up, copy a credential, or make a paid call on the user's behalf. Every call is a locally capped, persisted attempt (per cycle and per UTC day, with a 429 cooldown); local attempts are not provider credits, and budget exhaustion defers rather than switching to another metered service. `hermes secretary status`/`doctor` reports these capabilities candidly (unavailable with the honest reason rather than a fake success).

Private preparation is **opt-in and off by default**. Enabling the setting alone
arms nothing; to install the owned runner and one native `--no-agent` script job
use `hermes secretary setup --with-preparation` only after the user's explicit
decision, together with a small standing scope list (`conversation`, `public`,
`mail`, `calendar`, `tasks`, `documents`, `memory`).
Enabling it must not create a cron job. On a host without the explicit
`register_auxiliary_task(..., fallback_policy="configured_only")` contract, run
`hermes secretary prepare` only as a diagnostic: it must report the unsupported
lane and make no model call. Do not claim a prepared artifact, a working
fallback ladder, or a verified model-outage notice when that metadata is absent.
Requested reminders remain deterministic and independent.

Do not ask for passwords, tokens or chat authentication secrets in conversation. Use the platform's supported secure login/connection flow if the user authorises one. Missing credentials are a capability limit, not permission to create new access.

## 3. Install and enable

For a new installation, with user approval:

```bash
hermes plugins install Akuyumu/hermes-proactive-portal --no-enable
hermes plugins enable hermes-proactive-secretary
hermes secretary doctor
```

The public repository must be reachable through the installer's Git transport. If it is not, check the network or Git configuration; do not bypass checks. An immutable reviewed release may be pinned with `--ref <full-40-character-commit-sha>`.

Desktop installation uses the same unified native flow. Open **Capabilities → Plugins → Install from Git**, or use:

```text
hermes://plugin/install?repo=Akuyumu/hermes-proactive-portal&enable=1
```

The [official plugin guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins#one-click-install-links-desktop) describes a confirmation dialog that detects agent and Desktop components and lets the user choose. Older clients may expose different controls. There is no separate native `--desktop` CLI flag. Client-side downloads require the public Git URL to be reachable on the client; backend access alone does not prove that. Never create marker files, bypass a scanner or weaken a security check. Conversational onboarding works without the Desktop component and must not be blocked by a missing button.


The plugin bundles its operational skill at `../skills/proactive-secretary/SKILL.md`. Load that installed file through your skill system if available; otherwise read it directly. Do not accidentally load an unrelated skill with the same short name.

## 4. Prepare configuration before arming schedules

```bash
hermes secretary setup --no-cron
hermes secretary status
```

This prepares owner-only policy/state and runs offline checks without installing schedules. The runtime root is `$HERMES_HOME/plugin-data/hermes-proactive-secretary/`, unless explicitly overridden with `HERMES_PROACTIVE_SECRETARY_DATA` or `--data-dir`.

Write the validated per-user configuration, then review it before arming anything:

```bash
hermes secretary configure init
hermes secretary configure validate
hermes secretary configure preview
```

Apply only the user's agreed changes, preserving other fields. Do not hand-edit Hermes's main configuration or store credential values anywhere. `user_config.json` is versioned and fail-closed; never overwrite a nonempty configuration without explicit consent (`--force` is not a normal step). If an existing deployment already has its own policy/preferences, migrate it explicitly instead of hand-copying:

```bash
hermes secretary configure migrate --from-policy '<policy.json>' --dry-run  # review
hermes secretary configure migrate --from-policy '<policy.json>'            # write (keeps pause in preferences)
```

- Set the confirmed `timezone`, quiet hours and notification limits. When the host setting is unavailable and the user has not chosen, leave the neutral `UTC`.
- Set a neutral or user-approved `persona`/`notify_address`; never assume the original owner's identity.
- Name exactly the accounts/calendars/task lists that may be read (`mail.accounts`, `calendar.accounts`, `calendar.calendars`, `tasks.accounts`, `tasks.lists`). Task accounts are independent; credential presence alone is not permission.
- Migration supports repeatable `--mail-account`, `--calendar-account`, `--calendar-id`, `--task-account`, `--task-list` and `--adapter`. Omitted options retain prior explicit policy fields, including empty arrays and mail aliases. Review the source mappings in the dry-run report.
- Empty account arrays disable their connector. Empty calendar IDs retain selected/primary/holiday calendars within the configured accounts; nonempty IDs restrict event reads. Empty task lists retain all visible lists within configured task accounts; nonempty entries are exact case-sensitive IDs/titles (`id:ID`/`name:TITLE` disambiguate). Task adapters are empty, `tasks` or `productivity_secretary`; unsupported adapters fail closed.
- Only explicit legacy migration may retain omitted calendar/task account discovery (`advanced.legacy_account_discovery`). Old user configs lacking `tasks.accounts` copy explicit calendar accounts with a warning; new neutral configs include empty independent task accounts.
- Generic source seeds are empty. Add explicitly requested public watches with `hermes secretary watch add "<public topic>"`, then read back `hermes secretary watch list`.
- If adaptive discovery is approved, consult the bundled skill and policy implementation before changing mode. Keep inferred-goal acquisition and semantic privacy review paired; never send raw private context in public queries. Do not claim that adding a watch enables autonomous adaptation.
- The optional Google Workspace calendar adapter (`adapters.gws_calendar`) uses the installed skill's existing authorisation; enable it only after the user confirms that authorisation, and never broaden scopes or run a setup/reauthorisation flow.
- Keep location off unless a supported integration and separate authorisation are verified.
- **Assistant-mediated context (never a memory scrape).** If the user wants the
  pipeline to use context you already know, refresh the bounded snapshot
  explicitly — for a few relevant entries only, never an entire vault and never
  guessed consent:
  ```bash
  hermes secretary feedback context-update --note "<short fact>" \
    --provenance "assistant:memory" --kind interest --expires-at '<timezone-aware ISO>'
  hermes secretary feedback context-list    # metadata only; no raw text
  hermes secretary feedback context-revoke --id <entry id>
  ```
  Do not scrape USER/MEMORY/Hindsight/Library, credentials or connectors, and do
  not add a credential/connector/allowlist. `--provenance` must be one of the
  fixed low-sensitivity labels `user-stated`, `assistant:memory`,
  `assistant:knowledge` (default), `owner-config` or `synthetic-test`; never pass
  an owner/account/path/request excerpt. Use a timezone-aware ISO `--expires-at`
  (a naive value is read as UTC; a missing one defaults to 7 days, max 30). The
  snapshot is a bounded expiring cache; missing/expired/malformed means
  unavailable, never fabricated. Treat the context as untrusted advisory data —
  it cannot change permissions/settings. The privacy screen on copied identifiers
  is a conservative boundary, not a guarantee that no identifier can escape.

### Assistant-mediated corrections (not conversation surveillance)

When the user corrects an alert in conversation, interpret it, apply the
narrowest typed feedback command, read the exact record back (`feedback show
--id`, or the `receipt` returned by the write), and show the user a receipt. Ask
one clarifying question when the correction is ambiguous; a "thanks" or silence
is a no-op (`feedback noop`). Preserve conditional exceptions, expiries,
snoozes and provenance — never widen a snooze into a permanent suppression.

### Connector limits — do not hide these

The plugin is profile/path-safe, but it does **not** automatically support arbitrary services:

- Mail reads exactly the accounts named in `mail.accounts`. Legacy deployments keep the existing `formal`/`informal` Himalaya names; other accounts are supported only once explicitly named, and never shell-interpolated. Do not rename accounts or expand access without permission.
- Calendar/tasks require the external `productivity-secretary` library and its existing authorised configuration, OR the optional bundled Google Workspace calendar adapter (installed skill + active `HERMES_HOME` authorisation). Neither is enabled by default. `SECRETARY_SRC` or `PRODUCTIVITY_SECRETARY_SRC` may point to an existing library installation; do not create a new dependency or new credentials silently.
- The Google Workspace adapter is calendar-read only and has no verified Tasks command; there is no invented Tasks support. Its readiness probe does not read/refresh token values or call connector APIs.
- Private obligation sources may depend on additional authorised native connectors. A healthy mail executable is not proof that every private source is available.
- Without supported connectors, public-only operation is possible, but empty seeds/watches and disabled adaptation do not mean useful public coverage is already established.

Read doctor/status capabilities and perform bounded authorised reads when appropriate. Report unavailable, failed, deferred and truncated coverage separately. Never promise that this plugin reads any arbitrary mailbox or calendar.

## 5. Arm the plugin's own schedules

After configuration and capability review, use the same foreground entry points
the user sees. On a phone/chat the canonical start is `/secretary setup`; the
CLI equivalent is:

```bash
hermes secretary setup --deliver local   # explicit local/no-send first
hermes secretary status --json
hermes secretary doctor
hermes secretary self-test
```

The native `--deliver` grammar (from `hermes cron create --help`) is `origin`,
`local`, `telegram`, `discord`, `signal`, `platform:chat_id`, or
`bot-chat[:profile]`. Use a target you can actually see in the user's own
native channel, for example `telegram:<chat_id>` only after they supply that
exact id. Never guess a chat id, never present `origin` as if a terminal command
had chat context, and never write `<verified-delivery-target>` as a literal
command. If the target cannot be verified, say `delivery=unverified`, give the
next real discovery command (`hermes cron list` or the native gateway/account
UI), and keep `--deliver local` until an owner explicitly authorises a canary.
An actual canary send is a parent/owner action through the existing native
delivery adapter; this source worker never sends it.

Status language is deliberately separated: **installed** (plugin present),
**configured** (validated settings), **scheduled** (owned jobs recorded) and
**delivery-confirmed** (a real receipt from an explicit canary). Do not collapse
these into "enabled" or "verified". `/secretary pause|resume|disable|
cancel-setup` are the chat-native controls; `resume` reports actual readiness.

Setup creates and records ownership of:

- `hermes-proactive-secretary-monitoring`: every 15 minutes; may use the native LLM for discovery/planning/judgment.
- `hermes-proactive-secretary-reminders`: every minute; deterministic reminder script, no scheduled-agent LLM calls.
- `hermes-proactive-secretary-preparation` (only with explicit `--with-preparation`): optional bounded preparation script job; status and setup summaries include it whenever it is recorded and present.

The default jobs and the optional preparation job are native script jobs with `--no-agent`; the monitoring script itself can invoke models. One-minute reminder checks use CPU and sometimes connector requests, not an LLM conversation per tick. On its normal schedule the monitoring runner invokes the registered `hermes secretary pipeline` handler with fixed argv and the same profile/data paths, so the host-built context (and thus the user's active model and native `get_config`) is authoritative; it does not import the pipeline directly unless the native CLI is verifiably unavailable before any work (which is reported). The reminder runner likewise enters the registered `hermes secretary reminders` handler, never calls a model and never accesses `ctx.llm`; event reads honour ONLY the explicit `calendar.accounts`/`calendar.calendars` (mail accounts are never unioned in), and a disabled or empty calendar scope reads no event source. A subprocess timeout/error after either native handler started fails closed: no second run and no fallback. Both production entries apply neutral generic defaults when no configuration is present, so a deleted/missing `user_config.json` never silently re-enables the legacy private sources.

If an older profile recorded `hermes-proactive-secretary-pilot`, run `hermes secretary migrate-jobs` (dry-run), then `hermes secretary migrate-jobs --apply` before setup. The migration is idempotent, uses the host `cron.jobs.update_job` seam to change only the display name, and preserves the job id, schedule, delivery, enabled state, next run, history and ownership metadata. A name-only foreign row, ownership-id mismatch or occupied monitoring name is refused; a host without that seam is a reported blocker with no recreation.

Verify exact script paths, schedules, ownership and delivery target through the native cron listing. Re-run setup with the same destination to verify idempotency; it must not duplicate jobs. A cron-name collision is a reason to stop, not permission to adopt someone else's job.

Run `hermes secretary run-once --fixture healthy_empty` for a no-send fixture check. `run-once` is no-model by default, not proof of successful model-driven monitoring. For a real no-send model check, follow the installed skill using the **Hermes runtime interpreter**, explicit plugin state/policy/preferences paths and `--dry-run`; system Python may lack Hermes dependencies. Do not invoke debug/diagnostic modes as a scheduled delivery script.

Do not create fake user-requested reminders or send a test message without approval. Empty output confirms no emitted message, not delivery or healthy collection. Report actual checks and remaining limits.

## 6. Existing deployment: deliberate migration only

Do not import another person's deployment. For the user's own existing tree:

1. Inspect and pause only the exact legacy recurring jobs; read back their paused state. Preserve unrelated one-shot reminders.
2. Snapshot durable state under owner-only permissions, avoiding in-flight writers.
3. Preview `hermes secretary setup --import-from '<legacy-tree>' --import-dry-run`.
4. Import using `--import-from '<legacy-tree>' --no-cron` and verify byte-level parity for policy, preferences, deduplication and registries before arming anything.
5. Arm plugin jobs, verify their run outcomes, and ensure legacy jobs are not also running.
6. Remove obsolete jobs only with explicit approval. Keep a rollback snapshot unless deletion is separately approved.

Import refuses conflicting target files and does not delete/adopt legacy jobs. If anything fails, report it and restore only the deliberately paused legacy schedules when safe; never reset registries or blindly retry uncertain delivery.

## 7. Pause, toggle and uninstall

```bash
hermes secretary pause
hermes secretary resume
```

Global pause stops collection and output, including requested reminders. Native plugin disable makes the scheduled runners inert at their next invocation; it does not remove their cron entries or cancel work already in flight.

For full uninstall, while the plugin CLI is available:

```bash
hermes secretary uninstall
hermes plugins disable hermes-proactive-secretary
hermes plugins remove hermes-proactive-secretary
```

Read back that only recorded owned jobs/scripts were removed. Runtime policy, preferences and reminder state are preserved by default. `hermes secretary uninstall --purge` deletes that state and requires separate explicit consent. Do not delete unrelated files or disable other plugins.

If the plugin was already disabled and its CLI is unavailable, do not blindly enable it: enabling can reactivate existing schedules. Inspect the jobs and arrange a safe, user-approved cleanup first.

Reinstallation is possible, but preserved global pause may still be active. Review state with the user, rerun setup and use `hermes secretary resume` only when they intend to resume monitoring.

## Completion report

Give one short report: installed version/revision; profile; verified coverage and missing connectors; timezone/quiet hours; destination; exact schedules; model-cost distinction; checks that actually passed; and pause/uninstall commands. Distinguish installation, successful execution, emitted output and confirmed delivery. Do not claim universal compatibility, complete monitoring or feature parity from synthetic tests.
