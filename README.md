# Hermes Proactive Secretary

**A little less checking. A little more done.**

Hermes Proactive Secretary keeps Hermes useful between conversations. It works alongside your existing Hermes profile, follows the sources you authorise, prepares useful work, and brings you a short update when something deserves attention. It uses the same profile, model, skills, tools, connectors and memory as any other Hermes session.

![How the portal works: authorised sources feed a scheduled Hermes session that prepares useful work and proposes a notice through your existing notification gates](docs/assets/how-it-works.svg)

## What it does

- **Keeps watch over what matters.** Follow selected mail, calendars, tasks, public feeds and topics in one place.
- **Gives you a head start.** Prepare briefings, checklists and drafts for responsibilities you have accepted.
- **Suggests the next useful move.** Within your existing authority, it can notice a change, prepare the follow-up and suggest what to do next.
- **Carries corrections and follow-up into later runs.** Corrections stick, follow-ups stay open, and temporary scope stays temporary.
- **Respects your pace.** Quiet hours, cooldowns, an optional daily cap and simple pause controls.
- **Keeps you in control.** Every external send, publication, purchase, booking or calendar/task change still needs your approval through the normal Hermes gates.

For example, it can prepare a short checklist before a recurring meeting, flag a changed deadline in an authorised calendar, or draft a follow-up for something you asked it to track. These are illustrations. The portal only sees what your profile can already access.

## Current state

- Version: **1.0.7**
- Plugin id: `hermes-proactive-secretary`
- Command: `hermes secretary`
- `main` carries stable releases; `dev` is the active development branch.

## Install

```bash
hermes plugins install Akuyumu/hermes-proactive-portal --no-enable
hermes plugins enable hermes-proactive-secretary
hermes secretary doctor
```

Then prepare your first-run setup:

```bash
hermes secretary setup
```

The setup flow asks for the few choices it cannot safely guess, such as timezone, delivery and which sources you want it to read. You can start with local-only output and enable delivery later.

Desktop users can use the native consent link:

```text
hermes://plugin/install?repo=Akuyumu/hermes-proactive-portal&enable=1
```

The [Installation guide](docs/INSTALLATION.md) covers host compatibility, optional setup, upgrades and removal.

## Everyday controls

In chat:

```text
/secretary status
/secretary pause
/secretary resume
```

From the terminal:

```bash
hermes secretary status
hermes secretary job status
hermes secretary job pause
hermes secretary job resume
```

A global `hermes secretary pause` stops collection and future notices too.

## What it will not do

It will not send, publish, book, purchase or change your calendar or tasks without your approval. It will not read an account or source you have not authorised, and it does not create a second memory, permission system or approval path. It reports blocked, failed and deferred sources honestly rather than pretending coverage is complete.

## Learn more

- [Installation](docs/INSTALLATION.md)
- [Agent-assisted setup](docs/AGENT_SETUP.md)
- [Operations guide](docs/operations.md)
- [Background portal details](docs/NATIVE-PORTAL.md)
- [Engineering notes](docs/ENGINEERING.md)
- [Contributing](CONTRIBUTING.md)

## License

[MIT](LICENSE).
