# Contributing

Thanks for helping improve Hermes Proactive Portal. This is a Hermes plugin, not a separate assistant: it inherits the owning profile's tools, connectors, memory and approval rules. Please keep that boundary intact.

## Start here

- Read [Engineering](docs/ENGINEERING.md) for architecture, the native scheduled-session model, host prerequisites and the reviewed compatibility patch base.
- Read [Installation](docs/INSTALLATION.md) before testing a setup path.
- Read [docs/NATIVE-PORTAL.md](docs/NATIVE-PORTAL.md) for the portal job's lifecycle and delivery behaviour.
- For agent-assisted setup wording and consent rules, use [docs/AGENT_SETUP.md](docs/AGENT_SETUP.md) as the established reference.

## Branches and releases

`main` is the production/stable branch and the source of released builds. A `dev` branch, if one is used later, is experimental and is not the live install source. The release tag `v1.0.0` marks the tested `main` commit. When pinning a release, use the resolved immutable 40-character commit SHA with the native `--ref` argument; do not guess a branch or tag as a `--ref` value. Do not create release branches or automatic-update promises as part of a documentation or code change.

## Tests

Use a Python environment that already has the Hermes runtime and test dependencies. From the source tree:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m proactive_pilot.selftest
```

Tests are synthetic unless a change explicitly includes a reviewed host or device validation. A passing test suite does not prove live connector access, model availability, delivery or Desktop behaviour. Describe what was actually exercised, and keep unavailable, failed, deferred and truncated coverage separate.

## Native compatibility

Some Hermes versions need the included host compatibility update for the strict configured-only auxiliary lane. The reviewed base is `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`. Do not blindly apply the patch to a newer checkout, and do not treat it as universally portable. The safe check, apply and reverse procedure is in [Engineering](docs/ENGINEERING.md). A change must not weaken the configured-only contract or reintroduce a blanket route fallback.

## Scope and responsible changes

- Preserve existing approvals, private state, profile scope and owner-only paths. Do not add a parallel memory, permission, scheduling or credential store.
- Keep the portal's native final local. User notices go through the existing monitoring gates and `portal_propose_notice`; do not send through two paths.
- Do not broaden account, connector, calendar, task or public-source access without an explicit user decision.
- Do not add secrets, tokens, owner-specific identifiers, private filesystem roots or production summaries to code, tests, docs or examples.
- Never weaken the sanitiser, export policy, scanner checks or privacy boundaries to make an artifact pass.
- Keep prompts and documentation honest: a prompt is guidance, not enforcement, and native MCP tools retain their normal trust and approval semantics.
- Prefer small, reversible changes with tests and a clear rollback path. Do not reset or clean another owner's source tree.

## Packaging

The repository release exporter is `scripts/release_export.py`; the root `release_export.py` remains a compatibility shim for existing imports and commands. It builds a deterministic code-only archive and excludes tests, runtime state and `.build-context`. Repository `scripts/` tooling is not shipped, unlike the `runners/*.py` templates or the profile-installed scripts that setup places in the active profile. Its own scan and doctor output are packaging checks, not live qualification. See [operations](docs/operations.md#development-and-release) for the current commands and package naming.
