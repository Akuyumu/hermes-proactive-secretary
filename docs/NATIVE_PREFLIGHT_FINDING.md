# Native integration preflight

This checklist describes capability checks, **not a permission grant**. A note,
example or previous test in this repository never authorises another user's
connectors, model calls, metered requests or delivery target.

## Required checks

1. Load the plugin through the actual Hermes plugin manager in the intended
   profile. Verify its native CLI, chat command, tools and settings form.
2. Run `hermes portal doctor` and `hermes portal status --json`. Separate
   installed, configured, scheduled and delivery-verified states. An offline
   check or empty output is not proof of source health or successful delivery.
3. Before any model-dependent work, require the named
   `register_auxiliary_task(..., fallback_policy="configured_only")` contract.
   A permissive `**kwargs` signature is insufficient. Without this capability,
   keep model work blocked; do not substitute an unassigned model or recovery
   lane. Deterministic controls and requested reminders remain separate.
4. Verify only the source/account scopes the user explicitly approved. Missing
   optional connectors disable that capability rather than widening scope.
5. Keep private runtime state outside the installed code tree. Use an isolated
   profile and synthetic data for packaging and registration tests; reuse an
   already-provisioned Hermes runtime rather than silently installing another
   runtime inside the test profile.

## Optional metered retrieval

Firecrawl and other optional retrieval routes remain disabled without the
user's explicit service grant and configuration. Native MCP connection success
alone does not prove a plugin's scoped runtime can invoke a particular tool.
Verify the enabled server, allowed tool, current schema and native plugin call
path separately; do not introduce a parallel client or copy credentials.

A local attempt ceiling is not a provider credit balance or billing guarantee.
Before a real metered canary, obtain explicit permission for that call and its
cost boundary. If the supported native route is unavailable, report the named
capability as unavailable. Do not claim a scrape happened or broaden access to
make a check pass.

See [AGENT_SETUP.md](AGENT_SETUP.md) for installation, source configuration,
arming and verification. Live model, connector, messaging and physical-device
checks require their own evidence; packaging tests do not certify them.
