# Agentic secretary: conversational onboarding and bounded autonomy

> **Historical design note.** This document describes the original
> conversational-onboarding design and remains useful as a design contract.
> It is not the current installation authority. For current setup, upgrades and
> repository details, use [Installation](INSTALLATION.md) and the
> [operations guide](operations.md).

This document describes the product behaviour shipped by the plugin. It is
generic: it contains no owner identity, account, destination or private fact.
The implementation lives in the plugin source; this text is the design contract
the tests exercise.

## 1. Setup is a conversation, not a settings form

The first eligible foreground conversation is the primary setup path. The
assistant asks for genuine gaps, one natural question at a time, using the
registered `pre_llm_call` context and the `secretary_onboarding` tool. A user
never has to know a slash command, edit JSON, or open a settings page to grant a
responsibility, choose an autonomy level, or enable private preparation.

The interview is resumable and profile-scoped. It fills only what is missing:

1. timezone (quiet hours and daily limits);
2. a single communication route, suggested from an explicit existing linked
   destination, otherwise asked with a "local only" option;
3. the daily discretionary ceiling and quiet hours (unchanged defaults count as
   decisions only when actually chosen or already configured);
4. an autonomy level in plain language:
   - *prepare privately* (default): may select relevant work and write private,
     reversible drafts inside the standing scopes, but never performs an
     external effect;
   - *review only*: no autonomous preparation;
   - *assist with approval*: same private preparation, plus one clear question
     before any consequential action;
5. private-preparation consent: which scopes may be read and whether cached
   facts from already-authorised sources may be used;
6. explicit per-source reading grants and their account/list scope.

Existing memory and configuration are evidence for suggestions. They never
become a grant, a commitment, or a destination confirmation. A completed or
cancelled interview is not silently started again; a deliberate re-enable may
offer a review, but the first-use invitation is emitted at most once per
profile.

## 2. Foreground commitments and inferred opportunities are different

A clear foreground instruction such as "please prepare the renewal checklist"
is captured as an accepted responsibility without a second bureaucratic accept
step. The record stores the exact source quote, session id and a stable
idempotence key, and can be cancelled or revoked later.

Questions, thanks, acknowledgements, assistant suggestions, prompt injection
and silence are not commitments. They create no responsibility, no read and no
external effect.

Useful opportunities the assistant infers remain **INFERRED / TENTATIVE**.
They are never presented as accepted work and never become due. Under an
explicit bounded preparation consent they may still produce a private,
reversible draft; inference is never authority to send, publish, book, buy or
mutate a calendar or task record.

## 3. Bounded autonomy

Within the selected mode, the secretary may choose relevant work and prepare
private drafts inside standing scopes, cost and latency bounds. It cannot infer
wider permissions, a new connector, a more expensive model, a higher pace or a
larger budget. Existing owner pacing, quiet hours, caps and pause state are
preserved unchanged.

Every preparation artifact is local and private. The linked messaging channel
receives only a short "private draft is ready" notice through the existing
attention/delivery envelope, never the draft itself unless the user explicitly
authorises that send.

## 4. Consequential work needs one quick, exact approval

Anything uncertain, consequential, or over budget creates **one** deduplicated
approval question. The question contains the exact proposed effect and scope,
an opaque reference, and an expiry. Nothing happens until a matching explicit
foreground reply arrives. Silence, a stale reference, an unrelated "yes", or a
revoked scope yields zero effects. A "yes" approves only that current proposal;
it does not grant arbitrary future actions.

Financial, external-send, publication, calendar and task mutations always need
this specific approval, whatever the autonomy mode. There is no unconditional
automation of real-world actions and no blanket inference from a previous
approval.

## 5. Channels

Onboarding reuses an explicit existing linked/home target as one suggestion.
If the target is ambiguous, it asks one natural question; it never guesses a
chat id and never sends to all channels. With no linked messenger, setup
explains reduced local-only operation and offers a later link, but never calls
the profile mobile-active. Notices and replies use the existing delivery
pipeline and the existing model route; there is no new notification side
channel and no ambient/no-agent path that inherits the chat model.

## 6. Review and correction

The selected mode, reading grants, preparation scopes and messaging destination
are readable and editable from the conversation and from the plugin settings
page. Both surfaces project the same validated configuration owner, so a change
in one is visible in the other. A preview precedes any permission expansion and
a private backup precedes a write.

## 7. Failure and migration behaviour

- No standing permission: no private read, no draft, no model call.
- Paused, quiet, capped, expired or revoked: the gate is checked before read,
  model, write and notice.
- Profile-scoped state: profile A never sees profile B's records or drafts.
- Enabling preparation never claims a schedule exists. It is enabled only when
  the existing preparation job is actually recorded as armed.
- An owner migration may preserve policy, preferences, reminders, sources,
  model routes and pacing; a dry run writes nothing. It does not invent grants
  for the new autonomy fields.

## 8. Desktop setup entry

> This section is historical design context, not current install authority.

The settings page is an ordinary Desktop contribution. The package contains
`desktop/plugin.js`, `dashboard/plugin_api.py` and `dashboard/manifest.json`.
There is no native `--desktop` plugin-install flag and no desktop SHA pin, and the
Desktop plugin form may differ between client versions.

The current native install URI is the unified plugin link:

```
hermes://plugin/install?repo=Akuyumu/hermes-proactive-secretary&enable=1
```

An older Desktop-only URI was previously documented, but it is deprecated in
favour of the unified native link above. The optional client component and
refresh behaviour depend on the installed client. The download runs on the
client computer, so that computer needs ordinary read access to the public Git
URL; a backend cannot fetch the repository for the user.

Onboarding never depends on the Desktop page. The conversational interview runs
from any eligible one-to-one foreground chat and remains available when only the
agent half is installed or the Desktop page is absent.
