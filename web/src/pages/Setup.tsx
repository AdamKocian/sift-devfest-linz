// Connections, Guardrails and Settings: list -> detail.
import { useMemo, useState, type FormEvent } from 'react'
import { RiAddLine } from '@remixicon/react'
import { api } from '../api'
import * as f from '../format'
import { href, navigate } from '../router'
import { invalidate, useApi, useApp, type ThemePref } from '../store'
import type { Connection, Guardrail, SettingsValues } from '../types'
import {
  Button,
  CodeBlock,
  Dialog,
  EmptyState,
  ErrorNote,
  Group,
  Input,
  LinkButton,
  Loading,
  Page,
  PageHeader,
  Row,
  Segmented,
  Select,
  SettingRow,
  Status,
  Switch,
  Tag,
  Textarea,
  TextLink,
  type Tone,
} from '../ui'
import { BackOnMobile, ListDetail } from './common'

// --- connections ------------------------------------------------------------------------

const CONN_STATUS: Record<Connection['status'], [Tone, string]> = {
  fixture: ['gray', 'Fixture'],
  connected: ['success', 'Connected'],
  not_configured: ['away', 'Not set up'],
  dry_run: ['gray', 'Dry run'],
}

export function ConnectionsPage({ id }: { id?: string }) {
  const list = useApi('connections', api.connections)
  const items = list.data?.items ?? []
  const selected = items.find((c) => c.id === id) ?? (id ? undefined : items[0])
  return (
    <Page>
      <PageHeader title="Connections" subtitle="Where Sift reads from, thinks with and writes to." />
      {list.error && <ErrorNote error={list.error} onRetry={list.reload} />}
      {!list.data ? (
        !list.error && <Loading />
      ) : (
        <ListDetail
          hasSelection={Boolean(id)}
          list={
            <Group>
              {items.map((c) => (
                <Row
                  key={c.id}
                  href={href(`/connections/${c.id}`)}
                  selected={selected?.id === c.id}
                  title={c.name}
                  description={c.summary}
                  right={<Status tone={CONN_STATUS[c.status][0]}>{CONN_STATUS[c.status][1]}</Status>}
                />
              ))}
            </Group>
          }
          detail={
            selected ? (
              <div>
                <BackOnMobile to="/connections" label="Connections" />
                <h2 className="text-ui-lg font-semibold text-strong">{selected.name}</h2>
                <p className="mt-1 max-w-[60ch] text-ui text-sub">{selected.description}</p>
                <div className="mt-3">
                  <Status tone={CONN_STATUS[selected.status][0]}>{CONN_STATUS[selected.status][1]}</Status>
                  <span className="ml-2 text-ui text-sub">{selected.summary}</span>
                </div>
                <dl className="mt-5 divide-y divide-stroke overflow-hidden rounded-2xl bg-bg-white lg:rounded-none lg:border-y lg:border-stroke lg:bg-transparent">
                  {selected.fields.map((fl) => (
                    <div key={fl.label} className="flex items-center justify-between gap-4 px-4 py-3 text-ui lg:px-0 lg:py-2.5">
                      <dt className="font-mono text-[12px] text-sub">{fl.label}</dt>
                      <dd className="text-strong">{fl.value}</dd>
                    </div>
                  ))}
                </dl>
                {selected.note && <p className="mt-3 text-ui-sm text-soft">{selected.note}</p>}
                <p className="mt-6 max-w-[60ch] text-ui-sm text-soft">
                  Values come from environment variables on the server (see .env.example). Tokens are never shown.
                </p>
              </div>
            ) : (
              <EmptyState title="Not found" description="This connection does not exist." />
            )
          }
        />
      )}
    </Page>
  )
}

// --- guardrails -------------------------------------------------------------------------

const BUILTIN_RULE: Record<string, string> = {
  error_levels: 'loglevel in ("ERROR", "FATAL")',
  audit_logs: 'log source contains "audit"',
  referenced_90d: 'read by an alert, SLO or workflow in the last 90 days',
}

function matcherText(g: Guardrail): string {
  if (g.builtin && g.rule) return BUILTIN_RULE[g.rule] ?? g.rule
  const parts = []
  if (g.service) parts.push(`service == "${g.service}"`)
  if (g.kind) parts.push(`kind == "${g.kind}"`)
  if (g.key_pattern) parts.push(`key matches "${g.key_pattern}"`)
  return parts.join(' and ')
}

export function GuardrailsPage({ id }: { id?: string }) {
  const list = useApi('guardrails', api.guardrails)
  const items = list.data?.items ?? []
  const creating = id === 'new'
  const selected = creating ? undefined : (items.find((g) => g.id === id) ?? (id ? undefined : items[0]))
  return (
    <Page>
      <PageHeader
        title="Guardrails"
        subtitle="Data Sift never proposes to drop or sample, and why."
        actions={
          <LinkButton href={href('/guardrails/new')} className="w-full lg:w-auto">
            <RiAddLine />
            New guardrail
          </LinkButton>
        }
      />
      {list.error && <ErrorNote error={list.error} onRetry={list.reload} />}
      {!list.data ? (
        !list.error && <Loading />
      ) : (
        <ListDetail
          hasSelection={Boolean(id)}
          list={
            <Group>
              {items.map((g) => (
                <Row
                  key={g.id}
                  href={href(`/guardrails/${g.id}`)}
                  selected={selected?.id === g.id}
                  title={g.name}
                  description={g.reason}
                  meta={g.builtin ? 'Built-in' : `Added by ${g.created_by ?? 'someone'}`}
                  right={<span className="text-ui-sm text-soft tabular-nums">{g.matches.length}</span>}
                />
              ))}
            </Group>
          }
          detail={creating ? <NewGuardrail /> : selected ? <GuardrailDetail g={selected} /> : <EmptyState title="Not found" />}
        />
      )}
    </Page>
  )
}

function GuardrailDetail({ g }: { g: Guardrail }) {
  const { toast } = useApp()
  const [confirm, setConfirm] = useState(false)
  const total = g.matches.reduce((s, m) => s + m.cost_month, 0)
  const remove = async () => {
    try {
      await api.deleteGuardrail(g.id)
      invalidate()
      toast('Guardrail removed.')
      navigate('/guardrails')
    } catch (e) {
      toast((e as Error).message)
    }
    setConfirm(false)
  }
  return (
    <div>
      <BackOnMobile to="/guardrails" label="Guardrails" />
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-ui-lg font-semibold text-strong">{g.name}</h2>
          <p className="mt-1 max-w-[60ch] text-ui text-sub">{g.reason}</p>
        </div>
        {g.builtin ? <Tag>Built-in</Tag> : null}
      </div>
      <div className="mt-4">
        <div className="mb-1 text-ui-sm text-soft">Matches</div>
        <CodeBlock code={matcherText(g)} copy={false} />
      </div>
      <div className="mt-6 mb-2 flex items-baseline justify-between">
        <h3 className="text-ui font-semibold text-strong">
          Protects {f.plural(g.matches.length, 'signal')}
        </h3>
        {g.matches.length > 0 && <span className="text-ui text-sub">{f.eur(total)} a month at your rates</span>}
      </div>
      {g.matches.length === 0 ? (
        <p className="text-ui text-sub">Nothing matches right now.</p>
      ) : (
        <Group>
          {g.matches.map((m) => (
            <Row
              key={m.id}
              href={href(`/signals/${m.id}`)}
              title={m.key}
              description={`${m.service} · ${f.KIND_LABEL[m.kind]}`}
              right={<span className="tabular-nums">{f.eur(m.cost_month)}</span>}
            />
          ))}
        </Group>
      )}
      <p className="mt-4 text-ui-sm text-soft">
        {g.builtin ? 'Built into Sift. It cannot be removed.' : `Added by ${g.created_by} · ${f.dateTime(g.created_at)}`}
      </p>
      {!g.builtin && (
        <Button variant="danger" className="mt-4" onClick={() => setConfirm(true)}>
          Remove guardrail
        </Button>
      )}
      <Dialog
        open={confirm}
        onClose={() => setConfirm(false)}
        title="Remove this guardrail?"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirm(false)}>
              Cancel
            </Button>
            <Button variant="danger" onClick={remove}>
              Remove guardrail
            </Button>
          </>
        }
      >
        The agent may propose dropping {f.plural(g.matches.length, 'signal')} it protects today. Nothing is dropped
        without your approval.
      </Dialog>
    </div>
  )
}

function NewGuardrail() {
  const { toast } = useApp()
  const signals = useApi('guardrail:facets', () => api.signals())
  const facets = signals.data?.facets
  const [form, setForm] = useState({ name: '', reason: '', service: '', kind: '', key_pattern: '' })
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value })
  const preview = useMemo(() => {
    if (!signals.data) return []
    return signals.data.items.filter(
      (s) =>
        (!form.service || s.service === form.service) &&
        (!form.kind || s.kind === form.kind) &&
        (!form.key_pattern || globMatch(s.key, form.key_pattern)),
    )
  }, [signals.data, form.service, form.kind, form.key_pattern])
  const any = Boolean(form.service || form.kind || form.key_pattern)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const g = await api.addGuardrail({
        name: form.name,
        reason: form.reason,
        service: form.service || undefined,
        kind: form.kind || undefined,
        key_pattern: form.key_pattern || undefined,
      })
      invalidate()
      toast('Guardrail added.')
      navigate(`/guardrails/${g.id}`)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="max-w-lg space-y-4">
      <BackOnMobile to="/guardrails" label="Guardrails" />
      <h2 className="text-ui-lg font-semibold text-strong">New guardrail</h2>
      <label className="block space-y-1.5">
        <span className="text-ui font-medium text-strong">Name</span>
        <Input value={form.name} onChange={set('name')} required maxLength={120} placeholder="checkout order logs" />
      </label>
      <label className="block space-y-1.5">
        <span className="text-ui font-medium text-strong">Why it must be kept</span>
        <Textarea value={form.reason} onChange={set('reason')} required maxLength={500} placeholder="Finance reconciles orders from these." />
      </label>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="block space-y-1.5">
          <span className="text-ui font-medium text-strong">Service</span>
          <Select value={form.service} onChange={set('service')}>
            <option value="">Any</option>
            {(facets?.services ?? []).map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
        </label>
        <label className="block space-y-1.5">
          <span className="text-ui font-medium text-strong">Kind</span>
          <Select value={form.kind} onChange={set('kind')}>
            <option value="">Any</option>
            {(facets?.kinds ?? []).map((k) => (
              <option key={k} value={k}>
                {f.KIND_LABEL[k]}
              </option>
            ))}
          </Select>
        </label>
      </div>
      <label className="block space-y-1.5">
        <span className="text-ui font-medium text-strong">Key pattern</span>
        <Input value={form.key_pattern} onChange={set('key_pattern')} maxLength={200} placeholder="app · INFO or GET /static/*" />
        <span className="block text-ui-sm text-soft">* matches anything. Leave empty to protect every key.</span>
      </label>
      <p className="text-ui text-sub">
        {any ? `Would protect ${f.plural(preview.length, 'signal')} today.` : 'Pick a service, a kind or a key pattern.'}
      </p>
      {error && <p className="text-ui text-error-dark">{error}</p>}
      <div className="flex gap-2">
        <LinkButton href={href('/guardrails')} variant="ghost">
          Cancel
        </LinkButton>
        <Button type="submit" variant="primary" disabled={busy || !any || !form.name.trim() || !form.reason.trim()}>
          Add guardrail
        </Button>
      </div>
    </form>
  )
}

function globMatch(text: string, pattern: string): boolean {
  const re = new RegExp(`^${pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.')}$`)
  return re.test(text)
}

// --- settings ---------------------------------------------------------------------------

type SectionId = 'profile' | 'cost' | 'verification' | 'agent' | 'gemini' | 'appearance' | 'demo'

const SECTIONS: { id: SectionId; label: string; description: string; fixtureOnly?: boolean }[] = [
  { id: 'profile', label: 'Profile', description: 'The name approvals are recorded under.' },
  { id: 'cost', label: 'Cost model', description: 'Your € per GiB rates behind every estimate.' },
  { id: 'verification', label: 'Verification', description: 'How long Sift watches a rule before trusting it.' },
  { id: 'agent', label: 'Agent', description: 'When the agent runs and what it bothers you with.' },
  { id: 'gemini', label: 'Gemini usage', description: 'Your Vertex AI rate for the cost of each run.' },
  { id: 'appearance', label: 'Appearance', description: 'Light, dark or follow the system.' },
  { id: 'demo', label: 'Demo data', description: 'Reset the fixture world to its starting state.', fixtureOnly: true },
]

type Draft = SettingsValues

function diff(a: Draft, b: Draft): Partial<Draft> {
  const out: Record<string, unknown> = {}
  for (const k of Object.keys(b) as (keyof Draft)[]) {
    if (JSON.stringify(a[k]) !== JSON.stringify(b[k])) out[k] = b[k]
  }
  return out as Partial<Draft>
}

export function SettingsPage({ section }: { section?: string }) {
  const settings = useApi('settings', api.settings)
  const { meta } = useApp()
  const [draft, setDraft] = useState<Draft | null>(null)
  const saved = settings.data?.values
  const current = draft ?? saved
  const changes = saved && draft ? diff(saved, draft) : {}
  const dirty = Object.keys(changes).length > 0
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { toast } = useApp()
  const sections = SECTIONS.filter((s) => !s.fixtureOnly || meta?.mode === 'fixture')
  const active = (sections.find((s) => s.id === section) ?? (section ? undefined : sections[0]))?.id

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.saveSettings(changes)
      setDraft(null)
      invalidate()
      toast('Settings saved.')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  const update = (patch: Partial<Draft>) => setDraft({ ...(current as Draft), ...patch })

  return (
    <Page>
      <PageHeader title="Settings" subtitle="Changes are recorded in the activity log." />
      {settings.error && <ErrorNote error={settings.error} onRetry={settings.reload} />}
      {!current ? (
        !settings.error && <Loading />
      ) : (
        <ListDetail
          hasSelection={Boolean(section)}
          list={
            <Group>
              {sections.map((s) => (
                <Row
                  key={s.id}
                  href={href(`/settings/${s.id}`)}
                  selected={active === s.id}
                  title={s.label}
                  description={s.description}
                />
              ))}
            </Group>
          }
          detail={
            active ? (
              <div>
                <BackOnMobile to="/settings" label="Settings" />
                <h2 className="mb-3 text-ui-lg font-semibold text-strong">{SECTIONS.find((s) => s.id === active)!.label}</h2>
                <SettingsSection id={active} v={current} update={update} />
              </div>
            ) : (
              <EmptyState title="Not found" />
            )
          }
        />
      )}
      {dirty && (
        <div className="fixed inset-x-0 bottom-14 z-30 border-t border-stroke bg-bg-white pb-safe lg:bottom-0 lg:left-60">
          <div className="mx-auto flex max-w-[1120px] items-center gap-3 px-4 py-3 sm:px-6 lg:px-10">
            <span className="flex-1 text-ui text-strong">
              Unsaved changes{error && <span className="ml-2 text-error-dark">{error}</span>}
            </span>
            <Button variant="ghost" onClick={() => setDraft(null)}>
              Discard
            </Button>
            <Button variant="primary" onClick={save} disabled={busy}>
              Save
            </Button>
          </div>
        </div>
      )}
    </Page>
  )
}

function NumberInput({
  value,
  onChange,
  step = 0.1,
  min = 0,
  max,
  suffix,
  id,
  nullable,
}: {
  value: number | null
  onChange: (v: number | null) => void
  step?: number
  min?: number
  max?: number
  suffix?: string
  id?: string
  nullable?: boolean
}) {
  return (
    <span className="flex w-full items-center gap-2 sm:w-auto">
      <Input
        id={id}
        type="number"
        inputMode="decimal"
        step={step}
        min={min}
        max={max}
        value={value ?? ''}
        placeholder={nullable ? 'not set' : undefined}
        onChange={(e) => {
          const raw = e.target.value
          if (raw === '') return onChange(nullable ? null : 0)
          const n = Number(raw)
          if (!Number.isNaN(n)) onChange(n)
        }}
        className="text-right tabular-nums sm:w-28"
      />
      {suffix && <span className="shrink-0 text-ui-sm text-soft">{suffix}</span>}
    </span>
  )
}

function SettingsSection({ id, v, update }: { id: SectionId; v: Draft; update: (p: Partial<Draft>) => void }) {
  const { theme, setTheme, meta, toast } = useApp()
  const [confirmReset, setConfirmReset] = useState(false)
  const fixture = meta?.mode === 'fixture'

  if (id === 'profile') {
    return (
      <Group>
        <SettingRow
          label="Name"
          htmlFor="s-name"
          description="Approvals, rejections and reverts are recorded under this name. There is no login yet."
          control={<Input id="s-name" value={v.user_name} onChange={(e) => update({ user_name: e.target.value })} />}
        />
        <SettingRow
          label="Role"
          htmlFor="s-role"
          description="Shown next to your name in the sidebar."
          control={<Input id="s-role" value={v.user_role} onChange={(e) => update({ user_role: e.target.value })} />}
        />
      </Group>
    )
  }
  if (id === 'cost') {
    const rates = v.rates_eur_per_gib
    const row = (k: keyof typeof rates, label: string) => (
      <SettingRow
        key={k}
        label={label}
        htmlFor={`s-rate-${k}`}
        description={`Every € figure for ${label.toLowerCase()} multiplies GiB by this.`}
        control={
          <NumberInput
            id={`s-rate-${k}`}
            value={rates[k]}
            step={0.05}
            max={100}
            suffix="€ / GiB"
            onChange={(n) => update({ rates_eur_per_gib: { ...rates, [k]: n ?? 0 } })}
          />
        }
      />
    )
    return (
      <>
        <Group>
          {row('logs', 'Logs')}
          {row('spans', 'Spans')}
          {row('metrics', 'Metrics')}
          {row('events', 'Events')}
        </Group>
        <p className="mt-3 max-w-[60ch] text-ui-sm text-soft">
          Use your all-in rate: ingest, retention and query scans. These are your numbers, not a Dynatrace price list.
        </p>
      </>
    )
  }
  if (id === 'verification') {
    return (
      <Group>
        <SettingRow
          label="Verification window"
          htmlFor="s-window"
          description="After a rule is applied, Sift checks volume, references and Davis problems for this long, then marks it verified or reverts it."
          control={
            <NumberInput id="s-window" value={v.verify_window_min} step={5} min={1} max={1440} suffix="min" onChange={(n) => update({ verify_window_min: Math.round(n ?? 30) })} />
          }
        />
        {fixture && (
          <>
            <SettingRow
              label="Fixture verification delay"
              htmlFor="s-fixture-delay"
              description="Fixture mode finishes verification after this many seconds so the demo does not wait 30 minutes."
              control={
                <NumberInput id="s-fixture-delay" value={v.fixture_verify_seconds} step={1} min={1} max={600} suffix="s" onChange={(n) => update({ fixture_verify_seconds: n ?? 8 })} />
              }
            />
            <SettingRow
              label="Fail the next verification"
              description="The next rule fails its reference check and is reverted automatically. Use it to show auto-revert."
              control={
                <Switch
                  label="Fail the next verification"
                  checked={v.fixture_fail_next_verification}
                  onChange={(c) => update({ fixture_fail_next_verification: c })}
                />
              }
            />
          </>
        )}
      </Group>
    )
  }
  if (id === 'agent') {
    return (
      <Group>
        <SettingRow
          label="Run analysis every"
          htmlFor="s-every"
          description="The agent looks for new waste on this schedule. 0 means only when you start it."
          control={
            <NumberInput id="s-every" value={v.schedule_every_hours} step={1} min={0} max={168} suffix="hours" onChange={(n) => update({ schedule_every_hours: Math.round(n ?? 0) })} />
          }
        />
        <SettingRow
          label="Pause the agent"
          description="Stops scheduled runs. Rules already applied keep being verified."
          control={<Switch label="Pause the agent" checked={v.agent_paused} onChange={(c) => update({ agent_paused: c })} />}
        />
        <SettingRow
          label="Minimum saving"
          htmlFor="s-min"
          description="Unused data that would save less than this a month is not turned into a proposal."
          control={
            <NumberInput id="s-min" value={v.min_savings_eur_month} step={5} min={0} suffix="€ / month" onChange={(n) => update({ min_savings_eur_month: n ?? 0 })} />
          }
        />
      </Group>
    )
  }
  if (id === 'gemini') {
    const g = v.gemini_usd_per_m_tokens
    return (
      <>
        <Group>
          <SettingRow
            label="Input tokens"
            htmlFor="s-gin"
            description="Run details show an estimated Gemini cost once both rates are set."
            control={
              <NumberInput id="s-gin" nullable value={g.input} step={0.05} suffix="$ / 1M" onChange={(n) => update({ gemini_usd_per_m_tokens: { ...g, input: n } })} />
            }
          />
          <SettingRow
            label="Output tokens"
            htmlFor="s-gout"
            description="Copy both rates from the Vertex AI pricing page for the model you use."
            control={
              <NumberInput id="s-gout" nullable value={g.output} step={0.05} suffix="$ / 1M" onChange={(n) => update({ gemini_usd_per_m_tokens: { ...g, output: n } })} />
            }
          />
        </Group>
        <p className="mt-3 text-ui-sm text-soft">Every Gemini call goes to Vertex AI and is billed to the project in Connections.</p>
      </>
    )
  }
  if (id === 'appearance') {
    return (
      <Group>
        <SettingRow
          label="Theme"
          description="Applies right away and is remembered in this browser."
          control={
            <Segmented<ThemePref>
              items={[
                { value: 'system', label: 'System' },
                { value: 'light', label: 'Light' },
                { value: 'dark', label: 'Dark' },
              ]}
              value={theme}
              onChange={setTheme}
            />
          }
        />
      </Group>
    )
  }
  // demo
  const reset = async () => {
    try {
      await api.resetDemo()
      invalidate()
      toast('Demo data reset.')
      navigate('/')
    } catch (e) {
      toast((e as Error).message)
    }
    setConfirmReset(false)
  }
  return (
    <>
      <Group>
        <SettingRow
          label="Reset demo data"
          description="Puts every proposal, setting and activity entry back to the start of the demo. Same as make demo-reset."
          control={
            <Button variant="danger" onClick={() => setConfirmReset(true)}>
              Reset
            </Button>
          }
        />
      </Group>
      <p className="mt-3 text-ui-sm text-soft">
        Fixture mode runs fully offline on <span className="font-mono text-[12px]">fixtures/</span>. See{' '}
        <TextLink href={href('/connections')}>Connections</TextLink>.
      </p>
      <Dialog
        open={confirmReset}
        onClose={() => setConfirmReset(false)}
        title="Reset the demo data?"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmReset(false)}>
              Cancel
            </Button>
            <Button variant="danger" onClick={reset}>
              Reset demo data
            </Button>
          </>
        }
      >
        Approvals, rejections and setting changes made since the start of the demo are discarded. This only affects the
        local fixture database.
      </Dialog>
    </>
  )
}
