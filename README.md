# Hermes Proactive Portal

**A little less checking. A little more done.**

Hermes Proactive Portal helps Hermes stay useful between conversations. It works alongside your Hermes profile, follows the sources you have authorised, prepares useful work, and brings you a short update when something genuinely deserves attention.

Think of it as a private secretary with your existing access — not a separate assistant and not a second memory or permission system. It uses the same profile, model, skills, tools, connectors and memory as an ordinary Hermes session. Sources that are unavailable or not connected stay unavailable, and your existing approvals still apply.

Before installing, read [Installation](docs/INSTALLATION.md). It covers the host compatibility requirement and the optional host patch, so you can check compatibility before running any command.

![How the portal works: authorised sources feed a scheduled Hermes session that prepares useful work and proposes a notice through your existing notification gates](docs/assets/how-it-works.svg)

## What it does

- **Keeps watch over what matters.** Follow selected mail, calendars, tasks, public feeds and topics in one place.
- **Gives you a head start.** Prepare private briefings, checklists and drafts for responsibilities you have accepted.
- **Suggests the next useful move.** Within your existing authority, it can look beyond explicitly accepted responsibilities to notice a change, prepare the follow-up, and suggest what to do next.
- **Carries corrections and follow-up into later runs.** It keeps your correction and the follow-up, while temporary scope stays temporary.
- **Respects your pace.** Quiet hours, cooldowns, an optional daily cap and simple pause controls.

For example, it might prepare a short checklist before a recurring meeting, flag a changed deadline in an authorised calendar, or draft a follow-up for something you asked it to track. These are illustrations. The portal only sees what your profile can already access.

## Getting started

Install the plugin, enable it, then check the result:

```bash
hermes plugins install Akuyumu/hermes-proactive-secretary --no-enable
hermes plugins enable hermes-proactive-secretary
hermes secretary doctor
```

Prepare a local setup so you can try it without messaging anyone. This normal setup creates the routine monitoring and reminder schedules and keeps output local rather than sending it externally:

```bash
hermes secretary setup --deliver local
```

The background portal itself remains separately opt-in. Preview a schedule and enable it explicitly; nothing is created for the portal until you do. This example runs every two hours:

```bash
hermes secretary job preview --schedule "0 */2 * * *"
hermes secretary job enable --schedule "0 */2 * * *"
```

Setup does not activate the portal by itself, and private sources stay off until you choose them.

## Everyday controls

In chat:

```text
/secretary status
/secretary pause
/secretary resume
```

From the terminal:

```bash
hermes secretary job status
hermes secretary job pause
hermes secretary job resume
```

A global `hermes secretary pause` stops collection and future notices too. The portal's own final answer stays local; when something is worth telling you, it proposes a notice through the existing monitoring gates rather than sending through two paths.

## A note on installation

This is a real Hermes plugin, with a native CLI, tools and hooks. On some Hermes versions, the current deployment also needs the included host compatibility patch. You decide separately whether to apply that host update; without it, the strict model lane may be unavailable. See [Installation](docs/INSTALLATION.md) for the step-by-step setup and [Engineering](docs/ENGINEERING.md) for the host details.

The repository is private, so installation needs your existing GitHub access. There is no new account or OAuth connection. Desktop users can also use the native consent link:

```text
hermes://plugin/install?repo=Akuyumu/hermes-proactive-secretary&enable=1
```

The optional client component and refresh behaviour depend on your installed client. The [official plugin guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins) describes the native flow.

The native plugin id is `hermes-proactive-secretary`, and commands are `hermes secretary`; existing state is copied once from the legacy data root on first load. This release is version 1.0.6.

## Learn more

- [Installation](docs/INSTALLATION.md)
- [Agent-assisted setup](docs/AGENT_SETUP.md)
- [Repository layout](docs/ENGINEERING.md#repository-layout)
- [Contributing](CONTRIBUTING.md)
- [Operations guide](docs/operations.md)
- [Background portal details](docs/NATIVE-PORTAL.md)

## License

[MIT](LICENSE).
