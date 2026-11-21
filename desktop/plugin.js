/**
 * Proactive Secretary — supported Settings ▸ Plugins page.
 *
 * The page lives inside the native Settings rail (`ctx.registerSettingsPage`).
 * Every value it shows comes from the plugin's own authenticated REST backend
 * (`dashboard/plugin_api.py`, mounted at /api/plugins/hermes-proactive-secretary)
 * and therefore from the same validated configuration owner as chat onboarding
 * and the native Agent settings form. There is no second store and no hidden
 * toggle that could label a source "connected" by itself.
 *
 * The onboarding button only seats `/secretary setup` in the live composer (a
 * supported host door). If no composer answers, it says so and asks the user to
 * type the command — it never claims a conversation was launched.
 */
import { Badge, Button, ConfirmDialog, Input, ListRow, StatusDot, Switch, host, useQuery, useQueryClient } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState } from 'react'

const SOURCE_LABEL = {
  off: 'Off',
  unconfigured: 'Not configured',
  unavailable: 'Unavailable',
  unverified: 'Not verified',
  stale: 'Stale',
  fresh: 'Fresh'
}

const SOURCE_TONE = {
  off: 'muted',
  unconfigured: 'warn',
  unavailable: 'bad',
  unverified: 'warn',
  stale: 'warn',
  fresh: 'good'
}

const SOURCE_NAMES = [
  ['mail', 'Mail'],
  ['calendar', 'Calendar'],
  ['tasks', 'Tasks'],
  ['public', 'Public sources'],
  ['obligations', 'Mail obligations']
]

const ONBOARDING_TONE = {
  not_started: 'muted',
  in_progress: 'warn',
  cancelled: 'warn',
  pending_verification: 'warn',
  complete: 'good'
}

const SETUP_QUERY = ['proactive-secretary', 'setup-status']

function text(value, fallback = '') {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback
}

function statusMessage(error) {
  if (!error) return ''
  const raw = error instanceof Error ? error.message : String(error)
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed.message === 'string') return parsed.message
  } catch {
    // Not JSON: keep the bounded text below.
  }
  return raw.slice(0, 220)
}

function BadgeFor({ status }) {
  const key = status in SOURCE_LABEL ? status : 'unverified'
  return jsx(Badge, { variant: key === 'fresh' ? 'success' : key === 'unavailable' ? 'destructive' : key === 'off' ? 'muted' : 'warn', children: SOURCE_LABEL[key] })
}

function sourceDescription(row) {
  const count = Number(row && row.scope_count) || 0
  const scope = count === 0 ? 'no explicit sources selected' : `${count} explicit source${count === 1 ? '' : 's'} selected`
  if (row && row.status === 'off') return `Turned off. ${scope}.`
  if (row && row.status === 'unavailable') return `${text(row.remedy, 'The connector is unavailable on this machine.')} ${scope}.`
  if (row && row.status === 'unconfigured') return `Enabled, but ${scope}.`
  if (row && row.status === 'fresh') return `A recent read succeeded. ${scope}.`
  if (row && row.status === 'stale') return `The last successful read is stale. ${scope}.`
  return `No successful read is recorded yet. ${scope}.`
}

function SourceRows({ status }) {
  const sources = (status && status.sources) || {}
  return jsx('div', {
    children: SOURCE_NAMES.map(([key, label]) => {
      const row = sources[key] || { status: 'unverified' }
      return jsx(ListRow, {
        key,
        title: jsxs('span', {
          className: 'inline-flex items-center gap-2',
          children: [
            jsx(StatusDot, { tone: SOURCE_TONE[row.status] || 'warn' }),
            jsx('span', { children: label })
          ]
        }),
        description: sourceDescription(row),
        action: jsx(BadgeFor, { status: row.status })
      })
    })
  })
}

function SetupSection({ ctx, statusQuery }) {
  const queryClient = useQueryClient()
  const [resetOpen, setResetOpen] = useState(false)
  const [resetPlan, setResetPlan] = useState(null)
  const [phrase, setPhrase] = useState('')
  const [preparing, setPreparing] = useState(false)
  const [triggering, setTriggering] = useState(false)
  const [triggerNotice, setTriggerNotice] = useState('')

  const status = statusQuery.data || null
  const onboarding = (status && status.onboarding) || { status: 'not_started', label: 'Setup status unavailable' }
  const permissions = (status && status.permissions) || {}
  const errorText = statusQuery.error ? statusMessage(statusQuery.error) : ''

  async function prepareReset() {
    setPreparing(true)
    try {
      const plan = await ctx.rest('/setup/reset/prepare', { method: 'POST', body: {} })
      setResetPlan(plan)
      setPhrase('')
      setResetOpen(true)
    } catch (error) {
      host.notifyError(error, 'Could not prepare the reset. Nothing was changed.')
    } finally {
      setPreparing(false)
    }
  }

  async function confirmReset() {
    if (!resetPlan || !resetPlan.challenge) {
      throw new Error('The reset confirmation expired. Close this dialog and prepare it again.')
    }
    const expected = text(resetPlan.phrase, 'RESET')
    if (phrase !== expected) {
      throw new Error(`Type ${expected} exactly to confirm. Nothing was changed.`)
    }
    await ctx.rest('/setup/reset/commit', {
      method: 'POST',
      body: { challenge: resetPlan.challenge, phrase, explicit: true }
    })
    await statusQuery.refetch()
    queryClient.invalidateQueries({ queryKey: SETUP_QUERY })
    setResetOpen(false)
    setResetPlan(null)
    setPhrase('')
    host.notify({ kind: 'success', message: 'Reset complete. Reading permissions are off; send /secretary setup to re-confirm them.' })
  }

  async function triggerOnboarding() {
    setTriggering(true)
    setTriggerNotice('')
    try {
      const result = await ctx.rest('/setup/trigger', { method: 'POST', body: {} })
      const command = text(result && result.command, '/secretary setup')
      if (typeof host.composer?.insertText === 'function') {
        const placed = await host.composer.insertText(null, command, { mode: 'prefix' })
        if (placed) {
          host.composer.focus?.(null)
          setTriggerNotice(`Placed ${command} in your composer. Review and send it to start, resume or review setup.`)
          host.notify({ kind: 'success', message: 'Onboarding command placed in the composer. Review and send it.' })
        } else {
          setTriggerNotice(`No live composer was available. Type ${command} in a chat to continue. Nothing was changed.`)
        }
      } else {
        setTriggerNotice(`This Desktop build cannot place text in a composer. Type ${command} in a chat to continue. Nothing was changed.`)
      }
    } catch (error) {
      setTriggerNotice(`Could not prepare the continuation. Type /secretary setup in a chat. Nothing was changed.`)
      host.notifyError(error, 'Could not prepare the onboarding continuation.')
    } finally {
      setTriggering(false)
    }
  }

  const onboardingTone = ONBOARDING_TONE[onboarding.status] || 'warn'

  return jsxs('div', {
    className: 'space-y-6',
    children: [
      jsxs('div', {
        className: 'rounded-md border border-(--ui-stroke-secondary) p-4',
        children: [
          jsxs('div', {
            className: 'flex flex-wrap items-start justify-between gap-3',
            children: [
              jsxs('div', {
                className: 'min-w-0 space-y-1',
                children: [
                  jsxs('div', {
                    'aria-live': 'polite',
                    className: 'flex items-center gap-2',
                    role: 'status',
                    children: [
                      jsx(StatusDot, { tone: onboardingTone }),
                      jsx('span', { className: 'text-[length:var(--conversation-text-font-size)] font-medium', children: text(onboarding.label, 'Setup status unavailable') }),
                      permissions.needs_reconfirmation
                        ? jsx(Badge, { variant: 'warn', children: 'Permissions need confirmation' })
                        : null,
                      permissions.paused ? jsx(Badge, { variant: 'muted', children: 'Paused' }) : null
                    ]
                  }),
                  jsx('p', {
                    className: 'text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)',
                    children: permissions.needs_reconfirmation
                      ? 'Reading permissions are off. Setup will ask again before any source is read.'
                      : 'Setup status and source permissions come from the same validated configuration used at runtime.'
                  })
                ]
              }),
              jsxs('div', {
                className: 'flex flex-wrap items-center gap-2',
                children: [
                  jsx(Button, {
                    loading: triggering,
                    onClick: triggerOnboarding,
                    variant: 'secondary',
                    children: 'Trigger onboarding'
                  }),
                  jsx(Button, {
                    loading: preparing,
                    onClick: prepareReset,
                    variant: 'destructive',
                    children: 'Reset and onboard'
                  })
                ]
              })
            ]
          }),
          triggerNotice
            ? jsx('p', { className: 'mt-3 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-secondary)', role: 'status', children: triggerNotice })
            : null,
          errorText
            ? jsxs('div', {
                className: 'mt-3 space-y-2',
                role: 'alert',
                children: [
                  jsx('p', { className: 'text-[length:var(--conversation-caption-font-size)] text-destructive', children: `Status could not be read: ${errorText}` }),
                  jsx(Button, { onClick: () => statusQuery.refetch(), size: 'sm', variant: 'outline', children: 'Try again' })
                ]
              })
            : null
        ]
      }),
      jsx(SettingsEditor, { ctx, statusQuery }),
      jsxs('section', {
        className: 'space-y-1',
        children: [
          jsx('h3', { className: 'text-[length:var(--conversation-text-font-size)] font-medium', children: 'Everyday basics' }),
          jsx('p', { className: 'text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)', children: 'Values are shown as saved, not as manifest defaults. This editor reads and writes the same validated configuration as Agent settings and onboarding.' }),
          jsx(ListRow, { title: 'Timezone', description: text(status && status.config && status.config.timezone, 'Not available') }),
          jsx(ListRow, { title: 'Delivery', description: status && status.config && status.config.delivery_configured ? 'A delivery target is configured. Its saved value is shown below in Edit settings.' : 'No delivery target is configured yet.' }),
          jsx(ListRow, { title: 'Reading permissions', description: permissions.needs_reconfirmation ? 'Off. Send /secretary setup to re-confirm before any source is read.' : 'At least one source is enabled; see Sources for verification and freshness.' })
        ]
      }),
      jsxs('section', {
        className: 'space-y-1',
        children: [
          jsx('h3', { className: 'text-[length:var(--conversation-text-font-size)] font-medium', children: 'Sources at a glance' }),
          jsx(SourceRows, { status })
        ]
      }),
      jsx(ConfirmDialog, {
        open: resetOpen,
        onClose: () => {
          setResetOpen(false)
          setResetPlan(null)
          setPhrase('')
        },
        onConfirm: confirmReset,
        title: 'Reset setup and start again?',
        description: resetPlan && resetPlan.warning
          ? resetPlan.warning
          : 'This clears interview progress and everyday preferences, turns reading permissions off and starts setup again.',
        confirmLabel: 'Reset and continue',
        destructive: true,
        children: resetPlan
          ? jsxs('div', {
              className: 'space-y-3',
              children: [
                jsxs('div', {
                  children: [
                    jsx('p', { className: 'text-[length:var(--conversation-caption-font-size)] font-medium', children: 'Will be cleared' }),
                    jsx('ul', {
                      className: 'mt-1 list-disc space-y-1 pl-5 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-secondary)',
                      children: (resetPlan.cleared || []).map(item => jsx('li', { key: item, children: item }))
                    })
                  ]
                }),
                jsxs('div', {
                  children: [
                    jsx('p', { className: 'text-[length:var(--conversation-caption-font-size)] font-medium', children: 'Will be kept' }),
                    jsx('ul', {
                      className: 'mt-1 list-disc space-y-1 pl-5 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-secondary)',
                      children: (resetPlan.preserved || []).map(item => jsx('li', { key: item, children: item }))
                    })
                  ]
                }),
                jsx('p', {
                  className: 'text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)',
                  children: 'A private, owner-only backup is written before anything changes. You still need to re-confirm reading permissions in the next setup.'
                }),
                jsx(Input, {
                  'aria-label': 'Type RESET to confirm',
                  autoComplete: 'off',
                  onChange: event => setPhrase(event.target.value),
                  placeholder: `Type ${text(resetPlan.phrase, 'RESET')} to confirm`,
                  value: phrase
                })
              ]
            })
          : null
      })
    ]
  })
}

function SourcesPage({ ctx }) {
  const statusQuery = useQuery({
    queryKey: SETUP_QUERY,
    queryFn: () => ctx.rest('/setup/status'),
    refetchInterval: 15000
  })
  return jsxs('div', {
    className: 'space-y-4',
    children: [
      jsx('p', {
        className: 'text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)',
        children: 'Off is not connected. Not configured means enabled without an explicit source. Not verified, stale and fresh describe the last bounded read, never a switch position.'
      }),
      jsx(SourceRows, { status: statusQuery.data })
    ]
  })
}

function AdvancedPage({ ctx }) {
  const statusQuery = useQuery({
    queryKey: SETUP_QUERY,
    queryFn: () => ctx.rest('/setup/status'),
    refetchInterval: 15000
  })
  const status = statusQuery.data || {}
  const routes = status.model_routes || {}
  const jobs = status.jobs || {}
  const config = status.config || {}
  const primary = routes.primary || {}
  return jsxs('div', {
    className: 'space-y-1',
    children: [
      jsx(ListRow, {
        title: 'Configuration owner',
        description: config.error
          ? `Configuration could not be read (${config.error}). Nothing is displayed as an effective value.`
          : `${config.present ? 'Saved configuration' : 'Neutral defaults'} from ${text(config.source, 'unknown')}. Reset writes through this same owner.`
      }),
      jsx(ListRow, {
        title: 'Model route',
        description: primary.provider || primary.model
          ? `${text(primary.provider, 'provider not set')} / ${text(primary.model, 'model not set')}`
          : 'No secretary model route is configured. Reset does not change model routes.'
      }),
      jsx(ListRow, {
        title: 'Strict routing',
        description: routes.strict_active
          ? 'Configured-only routing is active.'
          : 'Strict configured-only routing is not active on this host. No fallback was widened by this page.'
      }),
      jsx(ListRow, {
        title: 'Reminders and jobs',
        description: `${Number(jobs.active_reminders) || 0} active reminder${Number(jobs.active_reminders) === 1 ? '' : 's'}, ${Number(jobs.owned_jobs) || 0} owned job${Number(jobs.owned_jobs) === 1 ? '' : 's'}. Reset keeps them.`
      }),
      jsx(ListRow, {
        title: 'Reset backup',
        description: 'A reset first writes an owner-only backup of this plugin\'s configuration, onboarding state and feedback preferences. It never copies credentials, host memories, reminders or other plugins\' data.'
      }),
      jsx(ListRow, {
        title: 'Trigger onboarding',
        description: 'Non-destructive. It places /secretary setup in the composer for review; it does not clear preferences and does not start an interview by itself.'
      })
    ]
  })
}


const LIST_FIELDS = ['public_interests', 'preparation_scopes']

function asList(value) {
  if (Array.isArray(value)) return value
  return String(value || '')
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)
}

function listText(value) {
  return typeof value === 'string' ? value : asList(value).join(', ')
}

function parsePublicSources(value) {
  if (Array.isArray(value)) return value
  const raw = String(value ?? '').trim()
  if (!raw) return []
  let parsed
  try {
    parsed = JSON.parse(raw)
  } catch {
    throw new Error('Public source entries must be a complete JSON list. Nothing was changed.')
  }
  if (!Array.isArray(parsed)) {
    throw new Error('Public source entries must be a JSON list. Nothing was changed.')
  }
  return parsed
}

function preparedValues(values) {
  const prepared = { ...values }
  for (const key of LIST_FIELDS) {
    if (key in prepared) prepared[key] = asList(prepared[key])
  }
  if ('public_sources' in prepared) prepared.public_sources = parsePublicSources(prepared.public_sources)
  return prepared
}

function sameValue(left, right) {
  if (Array.isArray(left) || Array.isArray(right)) return JSON.stringify(left) === JSON.stringify(right)
  return left === right
}

function changedPatch(baseValues, values) {
  const prepared = preparedValues(values)
  const patch = {}
  for (const [key, value] of Object.entries(prepared)) {
    if (!Object.prototype.hasOwnProperty.call(baseValues, key) || !sameValue(baseValues[key], value)) {
      patch[key] = value
    }
  }
  return patch
}

function SettingsEditor({ ctx, statusQuery }) {
  const [draft, setDraft] = useState(null)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState('')
  const [permissionOpen, setPermissionOpen] = useState(false)
  const [permissionPlan, setPermissionPlan] = useState(null)
  const settingsQuery = useQuery({
    queryKey: ['proactive-secretary', 'settings'],
    queryFn: () => ctx.rest('/settings'),
    refetchInterval: 30000
  })
  const data = settingsQuery.data || {}
  const values = draft ? draft.values : data.values || {}
  const disabled = settingsQuery.isLoading || Boolean(settingsQuery.error)

  function set(key, value) {
    setDraft(previous => (previous
      ? { ...previous, values: { ...previous.values, [key]: value } }
      : {
          revision: data.revision,
          baseValues: { ...(data.values || {}) },
          values: { ...(data.values || {}), [key]: value }
        }))
    setNotice('')
  }

  function discard() {
    setDraft(null)
    setNotice('')
    setPermissionOpen(false)
    setPermissionPlan(null)
    settingsQuery.refetch()
  }

  function textField(key, label, description, mode) {
    return jsx(ListRow, {
      key,
      title: label,
      description,
      action: jsx(Input, {
        'aria-label': label,
        disabled,
        value: mode === 'list' ? listText(values[key]) : String(values[key] ?? ''),
        onChange: event => {
          if (mode === 'list') return set(key, event.target.value)
          if (mode === 'optional-number') {
            // The optional cap is stored as blank (unset) or digit text, matching
            // the host schema's string field. A legacy saved integer is shown
            // readably and is only rewritten when the owner edits it.
            return set(key, event.target.value)
          }
          if (mode === 'number') {
            const next = event.target.value === '' ? 0 : Number(event.target.value)
            return set(key, Number.isFinite(next) ? next : 0)
          }
          return set(key, event.target.value)
        }
      })
    })
  }

  function switchField(key, label, description) {
    return jsx(ListRow, {
      key,
      title: label,
      description,
      action: jsx(Switch, {
        'aria-label': label,
        checked: Boolean(values[key]),
        disabled,
        onCheckedChange: checked => set(key, Boolean(checked))
      })
    })
  }

  async function applySave(acknowledged, snapshot, patch) {
    const active = snapshot || draft
    if (!active) return
    const result = await ctx.rest('/settings', {
      method: 'POST',
      body: {
        values: patch || changedPatch(active.baseValues, active.values),
        revision: active.revision,
        acknowledged_expanding: acknowledged
      }
    })
    setDraft(null)
    if (result && result.changed) {
      const widened = Array.isArray(result.expanding) && result.expanding.length > 0
      setNotice(widened
        ? `${result.explanation} Saved ${result.changed_fields.length} field${result.changed_fields.length === 1 ? '' : 's'}.`
        : `Saved ${result.changed_fields.length} field${result.changed_fields.length === 1 ? '' : 's'}.`)
      host.notify({ kind: 'success', message: 'Proactive Secretary settings saved.' })
    } else {
      setNotice('No values changed, so nothing was written.')
    }
    await Promise.all([settingsQuery.refetch(), statusQuery.refetch()])
  }

  async function save() {
    if (!draft) return
    setSaving(true)
    setNotice('')
    const active = draft
    let patch
    try {
      patch = changedPatch(active.baseValues, active.values)
    } catch (error) {
      setNotice(statusMessage(error) || 'Settings could not be saved. Nothing was changed.')
      setSaving(false)
      return
    }
    try {
      const preview = await ctx.rest('/settings/preview', {
        method: 'POST',
        body: { values: patch, revision: active.revision }
      })
      if (preview && preview.requires_confirmation) {
        setPermissionPlan({ ...preview, patch, draft: active })
        setPermissionOpen(true)
        return
      }
      await applySave([], active, patch)
    } catch (error) {
      setNotice(statusMessage(error) || 'Settings could not be saved. Nothing was changed.')
      host.notifyError(error, 'Could not save Proactive Secretary settings.')
    } finally {
      setSaving(false)
    }
  }

  async function confirmExpandedSave() {
    if (!permissionPlan || !permissionPlan.draft) {
      throw new Error('The permission preview expired. Press Save settings again. Nothing was changed.')
    }
    try {
      await applySave(permissionPlan.expanding || [], permissionPlan.draft, permissionPlan.patch)
    } catch (error) {
      throw new Error(statusMessage(error) || 'Settings could not be saved. Nothing was changed.')
    }
    setPermissionOpen(false)
    setPermissionPlan(null)
  }

  if (settingsQuery.error) {
    return jsxs('div', {
      role: 'alert',
      className: 'space-y-2 rounded-md border border-(--ui-stroke-secondary) p-3',
      children: [
        jsx('p', { children: `Settings could not be read: ${statusMessage(settingsQuery.error)}` }),
        jsx(Button, { onClick: () => settingsQuery.refetch(), size: 'sm', variant: 'outline', children: 'Try again' })
      ]
    })
  }

  return jsxs('div', {
    className: 'space-y-4 rounded-md border border-(--ui-stroke-secondary) p-4',
    children: [
      jsxs('div', {
        children: [
          jsx('h3', { className: 'text-[length:var(--conversation-text-font-size)] font-medium', children: 'Edit settings' }),
          jsx('p', {
            className: 'text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)',
            children: 'These are the same validated fields as the Agent settings form and onboarding. Changes are saved only when you press Save, never while you type.'
          })
        ]
      }),
      jsxs('section', {
        className: 'space-y-1',
        children: [
          jsx('h4', { className: 'font-medium', children: 'Setup' }),
          textField('timezone', 'Timezone', 'Used for quiet hours and the daily allowance.', 'text')
        ]
      }),
      jsxs('section', {
        className: 'space-y-1',
        children: [
          jsx('h4', { className: 'font-medium', children: 'Notification preferences' }),
          textField('destination', 'Where messages are sent', 'Empty records messages locally; it does not verify delivery.', 'text'),
          textField('daily_normal_cap', 'Most ordinary messages per day', '0 stops ordinary discretionary messages; leave blank for no daily ceiling. Requested reminders stay separate.', 'optional-number'),
          textField('quiet_start_hour', 'Quiet hours begin (0-23)', 'Ordinary messages wait from this local hour.', 'number'),
          textField('quiet_end_hour', 'Quiet hours end (0-23)', 'Ordinary messages may resume from this local hour.', 'number'),
          textField('cooldown_minutes', 'Minimum gap between messages (minutes)', 'A short breathing space between ordinary messages.', 'number')
        ]
      }),
      jsxs('section', {
        className: 'space-y-1',
        children: [
          jsx('h4', { className: 'font-medium', children: 'Sources' }),
          switchField('mail_enabled', 'Check mail', 'Only after an account is separately connected and scoped. Turning this on does not connect an account.'),
          switchField('calendar_enabled', 'Check calendar', 'Only after an account is separately connected and scoped.'),
          switchField('tasks_enabled', 'Check tasks', 'Only after a list is separately connected and scoped.'),
          switchField('public_feeds', 'Allow public-source checks', 'Reads only the public sources you listed; it adds no sources by itself.'),
          switchField('obligations', 'Check mail for due payments and orders', 'Uses accounts you already connected.'),
          textField('public_interests', 'Public topics you care about', 'Comma-separated plain topics. Empty stays empty.', 'list')
        ]
      }),
      jsxs('section', {
        className: 'space-y-1',
        children: [
          jsx('h4', { className: 'font-medium', children: 'Advanced' }),
          textField('discovery_mode', 'Public discovery (disabled, shadow or active)', 'Only public sources and topics are used.', 'text'),
          switchField('discovery_privacy_review', 'Extra privacy review for discovery', 'An optional semantic check, not a permission.'),
          switchField('discovery_inferred_goals', 'Allow guessed public goals', 'Inferred goals never become a standing permission.'),
          textField('collector_budget_seconds', 'Collection time limit (seconds)', 'Bound on one collection run.', 'number'),
          textField('max_candidates_per_kind', 'Most candidates kept per kind', 'A packet-size limit, not a result promise.', 'number'),
          textField('feed_max_sources_per_run', 'Public sources read per run', '0 reads none while keeping the list.', 'number'),
          switchField('preparation_enabled', 'Draft private preparation', 'Opt-in drafting for responsibilities you accepted.'),
          switchField('preparation_source_consent', 'Let preparation use already-checked facts', 'Only fresh facts from sources you enabled; no new connector.'),
          textField('preparation_mode', 'Autonomy mode', 'review_only, prepare_private or prepare_and_approve. Review-only blocks autonomous preparation.', 'text'),
          textField('preparation_scopes', 'What preparation may read', 'Comma-separated scopes such as conversation, public, mail, calendar, tasks.', 'list'),
          jsx(ListRow, {
            key: 'public_sources',
            title: 'Public source entries (advanced JSON)',
            description: 'Optional exact list of public sources. Leave [] for none; private account scope is never edited here.',
            action: jsx(Input, {
              'aria-label': 'Public source entries (advanced JSON)',
              disabled,
              value: typeof values.public_sources === 'string'
                ? values.public_sources
                : JSON.stringify(values.public_sources || []),
              onChange: event => {
                const raw = event.target.value
                try {
                  const parsed = JSON.parse(raw)
                  if (Array.isArray(parsed)) return set('public_sources', parsed)
                } catch {
                  // Keep the raw text so the user can finish typing; Save rejects it.
                }
                return set('public_sources', raw)
              }
            })
          })
        ]
      }),
      notice
        ? jsx('p', { role: 'status', className: 'text-[length:var(--conversation-caption-font-size)]', children: notice })
        : null,
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(Button, { disabled: !draft || saving, loading: saving, onClick: save, variant: 'primary', children: 'Save settings' }),
          jsx(Button, {
            disabled: !draft || saving,
            onClick: discard,
            variant: 'ghost',
            children: 'Discard changes'
          })
        ]
      }),
      jsx(ConfirmDialog, {
        open: permissionOpen,
        onClose: () => {
          setPermissionOpen(false)
          setPermissionPlan(null)
        },
        onConfirm: confirmExpandedSave,
        title: 'Confirm wider reading permission',
        description: 'Review the prospective expansion before it is applied. Nothing has been written yet.',
        confirmLabel: 'Confirm and save',
        destructive: true,
        children: permissionPlan
          ? jsxs('div', {
              className: 'space-y-3',
              children: [
                jsx('ul', {
                  className: 'list-disc space-y-1 pl-5 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-secondary)',
                  children: (permissionPlan.expansion_details || []).map(item => jsx('li', {
                    key: item.key,
                    children: jsxs('span', {
                      children: [
                        jsx('span', { className: 'font-medium', children: item.label }),
                        jsx('span', { children: `: ${item.description}` })
                      ]
                    })
                  }))
                }),
                jsx('p', {
                  className: 'text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)',
                  children: 'These changes use only accounts, lists and source entries you already connected. A checkbox never connects an external account, and onboarding still confirms any remaining scope.'
                })
              ]
            })
          : null
      })
    ]
  })
}

function SetupPage({ ctx }) {
  const statusQuery = useQuery({
    queryKey: SETUP_QUERY,
    queryFn: () => ctx.rest('/setup/status'),
    refetchInterval: 15000
  })
  return jsx(SetupSection, { ctx, statusQuery })
}

export default {
  id: 'hermes-proactive-secretary',
  name: 'Proactive Secretary',
  register(ctx) {
    ctx.registerSettingsPage?.({
      id: 'settings',
      title: 'Proactive Secretary',
      icon: 'checklist',
      order: 20,
      render: () => jsx(SetupPage, { ctx }),
      children: [
        { id: 'sources', title: 'Sources', render: () => jsx(SourcesPage, { ctx }) },
        { id: 'advanced', title: 'Advanced', render: () => jsx(AdvancedPage, { ctx }) }
      ]
    })
  }
}
