# Installing Hermes Proactive Secretary

This guide is for the person using Hermes. It covers a normal installation, the optional host compatibility update, first setup and upgrades. Developer and host-level detail lives in [Engineering](ENGINEERING.md).

Hermes Proactive Secretary is version **1.1.3**. The plugin id is `hermes-proactive-secretary` and the commands are `hermes secretary`. On first load after an upgrade, existing state under `plugin-data/hermes-proactive-portal/` is copied once and idempotently into the new `plugin-data/hermes-proactive-secretary/` root; the legacy tree is left untouched as a rollback copy. Old owner-facing cron jobs are retired by `setup` (and the portal job by `hermes secretary job enable`) using the ownership ledger, then recreated under the new names. If you had enabled the old portal job, re-run `hermes secretary job enable --schedule "<your schedule>"` after the upgrade to recreate it under the new name.

## 1. Check host compatibility first

Before installing, confirm that:

- Hermes can install and enable plugins and exposes native `hermes cron` commands;
- you are using a Linux or macOS host that you are willing to change; Windows and fresh-machine portability are not independently certified;
- you know which Hermes profile (`HERMES_HOME`) should receive the portal.

The portal uses the same profile, model, tools, connectors, skills and memory as an ordinary Hermes session. Installation does not connect any account, broaden any permission or create a new OAuth connection.

Some Hermes versions also need the included host compatibility update so the strict configured-only model lane is available. That update is a change to the Hermes host, not to this plugin. Apply it only as a separate, conscious decision with explicit authority to change the host. The reviewed patch is [docs/compatibility/hermes-host-portal.patch](compatibility/hermes-host-portal.patch), and it targets Hermes base `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`. Do not copy or apply it blindly to a newer checkout; re-review it against your actual host source first. The safe check, apply and reverse procedure is in [Engineering](ENGINEERING.md#host-compatibility-patch).

The current host has been validated with this package, but no fresh-machine certification is claimed. If you are unsure whether your host needs the patch, ask your Hermes agent to inspect the host and report the compatibility finding before changing anything.

## 2. Confirm repository access

The repository is public at `Akuyumu/hermes-proactive-secretary`. Installation uses the public Git URL, and no Git access grant is required beyond ordinary read access. There is no new account or new credential flow in this guide. If Git cannot reach the public repository, check your network or Git configuration before retrying.

## 3. Install and enable the plugin

With your approval, run:

```bash
hermes plugins install Akuyumu/hermes-proactive-secretary --no-enable
hermes plugins enable hermes-proactive-secretary
hermes secretary doctor
```

For an immutable reviewed release, you may pin the resolved full 40-character commit SHA with the native `--ref` argument. Use the actual resolved SHA, not a guessed branch or tag as the ref value.

## 4. Prepare setup without sending anything

Start with a no-send setup that also skips schedules:

```bash
hermes secretary setup --no-cron --deliver local
hermes secretary status
```

This prepares owner-only policy and state and runs offline checks without installing schedules or messaging anyone. `--deliver local` means output is not sent externally; it does not mean "no schedules" — `--no-cron` is what skips schedule creation. This stage is not full monitoring setup. Private sources are off by default; choose only the accounts, calendars, task lists and public sources you want the portal to read.

## 5. Try it, then arm the monitoring lane and portal

Run a bounded check:

```bash
hermes secretary run-once --fixture healthy_empty
hermes secretary self-test
```

An empty result means no message was emitted; it is not proof of healthy collection or delivery.

Before enabling the portal, arm the normal monitoring and reminder schedules that portal notices depend on:

```bash
hermes secretary setup --deliver local
```

Normal `setup` creates those schedules; `--deliver local` keeps output local rather than sending it externally. Review your sources and preferences first, and keep private sources off until you explicitly consent to them. The background portal itself remains separately opt-in. Preview a schedule first, then enable it explicitly. This is an example schedule — every two hours — not a built-in default:

```bash
hermes secretary job preview --schedule "0 */2 * * *"
hermes secretary job enable --schedule "0 */2 * * *"
```

Setup does not activate the portal by itself. You can inspect, pause or resume only the portal with:

```bash
hermes secretary job status
hermes secretary job pause
hermes secretary job resume
```

Use `hermes secretary pause` and `hermes secretary resume` for the global pause, which also stops collection and requested reminders.

## 6. Optional Desktop install

On Desktop, the native plugin flow is **Capabilities → Plugins → Install from Git**, or the consent link:

```text
hermes://plugin/install?repo=Akuyumu/hermes-proactive-secretary&enable=1
```

The optional client component and refresh behaviour depend on your installed client; this guide does not claim parity across clients. The [official plugin guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins) describes the native flow. Conversational setup works without the Desktop component.

## 7. Upgrading

`main` is the production/stable branch and the source of released installs; `dev` is the active development branch. Release tags mark tested `main` commits. For a controlled pin, use the resolved immutable 40-character SHA with `--ref`.

There is no automatic-update promise. After a Hermes upgrade:

1. Re-check whether the included host compatibility patch still applies to the new host version. Do not assume it carries over.
2. Re-run `hermes secretary doctor` and review the result.
3. Review `hermes secretary job status` before resuming normal schedules.

If the host source changed, re-review the patch instead of applying an old diff. Keep a known-good rollback of your own host change.

## 8. Pause or remove

```bash
hermes secretary disable
hermes plugins disable hermes-proactive-secretary
hermes plugins remove hermes-proactive-secretary
```

This removes only plugin-owned jobs and runners and preserves private runtime data by default. `hermes secretary uninstall --purge` deletes runtime state and requires separate explicit consent. Reinstallation can preserve a previous global pause, so review state and resume deliberately.

For agent-assisted installation, see [AGENT_SETUP.md](AGENT_SETUP.md). For architecture, host prerequisites and patch rollback, see [Engineering](ENGINEERING.md).
