// App shell: sidebar (desktop), header + tab bar (mobile), ⌘K palette, environment switcher.
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import {
  RiArrowRightSLine,
  RiNotification3Line,
  RiChat3Line,
  RiExpandUpDownLine,
  RiFileList3Line,
  RiHistoryLine,
  RiHome5Line,
  RiMenuLine,
  RiMoonLine,
  RiPlugLine,
  RiSearchLine,
  RiSettings3Line,
  RiShieldCheckLine,
  RiStackLine,
  RiSunLine,
} from '@remixicon/react'
import { api } from './api'
import * as f from './format'
import { href, navigate, useRoute } from './router'
import { useApi, useApp } from './store'
import { AgentMark, Avatar, BottomSheet, Button, cn, Dot, Mark, MenuItem, Popover, Tag } from './ui'

// --- navigation model -------------------------------------------------------------------

interface NavItem {
  path: string
  label: string
  icon: typeof RiHome5Line
}

const MAIN: NavItem[] = [
  { path: '/', label: 'Overview', icon: RiHome5Line },
  { path: '/proposals', label: 'Proposals', icon: RiFileList3Line },
  { path: '/signals', label: 'Signals', icon: RiStackLine },
  { path: '/activity', label: 'Activity', icon: RiHistoryLine },
]

const SETUP: NavItem[] = [
  { path: '/connections', label: 'Connections', icon: RiPlugLine },
  { path: '/guardrails', label: 'Guardrails', icon: RiShieldCheckLine },
  { path: '/settings', label: 'Settings', icon: RiSettings3Line },
]

function isActive(path: string, current: string): boolean {
  return path === '/' ? current === '/' : current === path || current.startsWith(`${path}/`)
}

// --- environment ------------------------------------------------------------------------

function EnvList({ onPick }: { onPick: () => void }) {
  const { meta } = useApp()
  if (!meta) return null
  return (
    <div>
      <MenuItem selected onClick={onPick} meta={meta.mode === 'fixture' ? 'fixture' : 'live'}>
        <span className="font-mono text-[12px]">{meta.environment.id}</span>
        <span className="text-sub">{meta.environment.name}</span>
      </MenuItem>
      <div className="mt-1 border-t border-stroke px-2 pt-2 pb-1.5 text-ui-sm text-sub">
        Live environments are added in{' '}
        <a className="text-primary hover:underline" href={href('/connections/dynatrace')} onClick={onPick}>
          Connections
        </a>
        .
      </div>
    </div>
  )
}

function EnvSwitcher() {
  const { meta } = useApp()
  return (
    <Popover
      className="w-64"
      trigger={({ open, toggle }) => (
        <button
          type="button"
          onClick={toggle}
          aria-expanded={open}
          aria-label="Switch environment"
          className="flex h-11 w-full items-center gap-2.5 rounded-md px-1.5 text-left outline-none hover:bg-bg-soft/60 focus-visible:ring-2 focus-visible:ring-primary/40 dark:hover:bg-bg-surface"
        >
          <Mark />
          <span className="min-w-0 flex-1">
            <span className="block text-ui font-semibold text-strong">Sift</span>
            <span className="block truncate font-mono text-[11px] leading-4 text-soft">
              {meta ? `${meta.environment.id}${meta.mode === 'fixture' ? ' · fixture' : ''}` : '…'}
            </span>
          </span>
          <RiExpandUpDownLine className="size-4 shrink-0 text-soft" />
        </button>
      )}
    >
      {(close) => <EnvList onPick={close} />}
    </Popover>
  )
}

// --- notifications ----------------------------------------------------------------------

const NOTABLE = new Set(['verified', 'verification_failed', 'reverted', 'run_finished', 'run_failed', 'proposal_created'])

function Notifications({ side = 'top' }: { side?: 'top' | 'bottom' }) {
  const activity = useApi('activity:notify', () => api.activity({ limit: 40 }), 15000)
  const items = (activity.data?.items ?? []).filter((a) => NOTABLE.has(a.action)).slice(0, 6)
  return (
    <Popover
      side={side}
      align={side === 'top' ? 'start' : 'end'}
      className="w-80"
      trigger={({ open, toggle }) => (
        <Button variant="ghost" size="icon" onClick={toggle} aria-expanded={open} aria-label="Notifications">
          <RiNotification3Line />
        </Button>
      )}
    >
      {(close) => (
        <div>
          <div className="px-2 pt-1.5 pb-1 text-ui-sm text-soft">Recent</div>
          {items.length === 0 && <div className="px-2 py-3 text-ui text-sub">Nothing new.</div>}
          {items.map((a) => (
            <a
              key={a.id}
              href={a.subject_type === 'proposal' && a.subject_id ? href(`/proposals/${a.subject_id}`) : href('/activity')}
              onClick={close}
              className="flex gap-2 rounded-md px-2 py-2 text-ui outline-none hover:bg-bg-weak focus-visible:bg-bg-weak"
            >
              <Dot
                tone={a.action === 'verification_failed' || a.action === 'run_failed' ? 'error' : a.action === 'verified' ? 'success' : 'gray'}
                className="mt-1.5"
              />
              <span className="min-w-0 flex-1">
                <span className="line-clamp-2 text-strong">{a.summary}</span>
                <span className="text-ui-sm text-soft">{f.ago(a.at)}</span>
              </span>
            </a>
          ))}
          <div className="mt-1 border-t border-stroke pt-1">
            <MenuItem href={href('/activity')}>View all activity</MenuItem>
          </div>
        </div>
      )}
    </Popover>
  )
}

function ThemeButton() {
  const { dark, setTheme } = useApp()
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={() => setTheme(dark ? 'light' : 'dark')}
      aria-label={dark ? 'Switch to light mode' : 'Switch to dark mode'}
    >
      {dark ? <RiSunLine /> : <RiMoonLine />}
    </Button>
  )
}

// --- sidebar ----------------------------------------------------------------------------

function NavLink({ item, current, count }: { item: NavItem; current: string; count?: number }) {
  const active = isActive(item.path, current)
  const Icon = item.icon
  return (
    <a
      href={href(item.path)}
      aria-current={active ? 'page' : undefined}
      className={cn(
        'flex h-8 items-center gap-2.5 rounded-md px-2 text-ui outline-none transition-colors focus-visible:ring-2 focus-visible:ring-primary/40',
        active
          ? 'bg-bg-soft font-medium text-strong dark:bg-bg-surface'
          : 'text-sub hover:bg-bg-soft/60 hover:text-strong dark:hover:bg-bg-surface/60',
      )}
    >
      <Icon className="size-4 shrink-0 text-soft" />
      <span className="flex-1">{item.label}</span>
      {count !== undefined && count > 0 && <span className="text-ui-sm text-soft tabular-nums">{count}</span>}
    </a>
  )
}

function Sidebar() {
  const route = useRoute()
  const { meta, setPaletteOpen } = useApp()
  return (
    <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-stroke bg-bg-sidebar lg:flex">
      <div className="px-3 pt-3">
        <EnvSwitcher />
        <button
          type="button"
          onClick={() => setPaletteOpen(true)}
          className="mt-3 flex h-8 w-full items-center gap-2 rounded-md border border-stroke bg-bg-white px-2 text-left text-ui text-soft outline-none hover:text-sub focus-visible:ring-2 focus-visible:ring-primary/40"
        >
          <RiSearchLine className="size-4 shrink-0" />
          <span className="flex-1 truncate">Ask Sift or search…</span>
          <kbd className="font-sans text-ui-sm text-soft">⌘K</kbd>
        </button>
      </div>
      <nav className="mt-4 flex-1 space-y-0.5 overflow-y-auto px-3" aria-label="Main">
        {MAIN.map((item) => (
          <NavLink
            key={item.path}
            item={item}
            current={route.path}
            count={item.path === '/proposals' ? meta?.awaiting_count : undefined}
          />
        ))}
        <div className="px-2 pt-5 pb-1.5 text-ui-sm text-soft">Setup</div>
        {SETUP.map((item) => (
          <NavLink key={item.path} item={item} current={route.path} />
        ))}
      </nav>
      <div className="flex items-center gap-1 border-t border-stroke px-3 py-2.5">
        <a
          href={href('/settings/profile')}
          className="flex min-w-0 flex-1 items-center gap-2 rounded-md px-1 py-1 outline-none hover:bg-bg-soft/60 focus-visible:ring-2 focus-visible:ring-primary/40 dark:hover:bg-bg-surface/60"
        >
          <Avatar name={meta?.user.name ?? '?'} />
          <span className="min-w-0">
            <span className="block truncate text-ui font-medium text-strong">{meta?.user.name ?? '…'}</span>
            <span className="block truncate text-ui-sm text-soft">{meta?.user.role}</span>
          </span>
        </a>
        <Notifications />
        <ThemeButton />
      </div>
    </aside>
  )
}

// --- mobile -----------------------------------------------------------------------------

function MobileHeader() {
  const { meta } = useApp()
  const [sheet, setSheet] = useState(false)
  return (
    <header className="sticky top-0 z-20 flex h-14 items-center gap-2 border-b border-stroke bg-bg-canvas px-4 lg:hidden">
      <button
        type="button"
        onClick={() => setSheet(true)}
        className="flex min-w-0 flex-1 items-center gap-2.5 text-left outline-none"
        aria-label="Switch environment"
      >
        <Mark />
        <span className="min-w-0">
          <span className="block text-ui leading-5 font-semibold text-strong">Sift</span>
          <span className="block truncate font-mono text-[12px] leading-4 text-soft">
            {meta ? `${meta.environment.id}${meta.mode === 'fixture' ? ' · fixture' : ''}` : '…'}
          </span>
        </span>
        <RiExpandUpDownLine className="size-4 shrink-0 text-soft" />
      </button>
      <Notifications side="bottom" />
      <BottomSheet open={sheet} onClose={() => setSheet(false)} title="Environment">
        <div className="rounded-2xl bg-bg-white p-1">
          <EnvList onPick={() => setSheet(false)} />
        </div>
      </BottomSheet>
    </header>
  )
}

function TabBar() {
  const route = useRoute()
  const { meta } = useApp()
  const tabs: { path: string; label: string; icon: typeof RiHome5Line; badge?: number }[] = [
    { path: '/', label: 'Overview', icon: RiHome5Line },
    { path: '/proposals', label: 'Proposals', icon: RiFileList3Line, badge: meta?.awaiting_count },
    { path: '/ask', label: 'Ask', icon: RiChat3Line },
    { path: '/menu', label: 'Menu', icon: RiMenuLine },
  ]
  const menuPaths = ['/menu', '/signals', '/activity', '/connections', '/guardrails', '/settings']
  return (
    <nav
      aria-label="Main"
      className="fixed inset-x-0 bottom-0 z-20 grid grid-cols-4 border-t border-stroke bg-bg-white pb-safe lg:hidden"
    >
      {tabs.map((t) => {
        const active =
          t.path === '/menu' ? menuPaths.some((p) => isActive(p, route.path)) : isActive(t.path, route.path)
        const Icon = t.icon
        return (
          <a
            key={t.path}
            href={href(t.path)}
            aria-current={active ? 'page' : undefined}
            className={cn(
              'relative flex h-14 flex-col items-center justify-center gap-0.5 text-[12px] font-medium outline-none',
              active ? 'text-primary' : 'text-sub',
            )}
          >
            <span className="relative">
              <Icon className="size-6" />
              {t.badge ? (
                <span className="absolute -top-1 left-4 min-w-4 rounded-full bg-bg-strong px-1 text-center text-[10px] leading-4 font-semibold text-bg-white tabular-nums">
                  {t.badge}
                </span>
              ) : null}
            </span>
            {t.label}
          </a>
        )
      })}
    </nav>
  )
}

// --- command palette --------------------------------------------------------------------

interface Command {
  id: string
  group: string
  label: ReactNode
  hint?: string
  run: () => void
}

function CommandPalette() {
  const { paletteOpen, setPaletteOpen, openChat, startRun } = useApp()
  const [q, setQ] = useState('')
  const [active, setActive] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const signals = useApi(paletteOpen ? 'palette:signals' : null, () => api.signals())
  const proposals = useApi(paletteOpen ? 'palette:proposals' : null, () => api.proposals())

  const close = () => {
    setPaletteOpen(false)
    setQ('')
    setActive(0)
  }

  const commands = useMemo<Command[]>(() => {
    const needle = q.trim().toLowerCase()
    const match = (s: string) => !needle || s.toLowerCase().includes(needle)
    const out: Command[] = []
    out.push({
      id: 'ask',
      group: 'Ask',
      label: (
        <>
          Ask Sift{q.trim() ? ': ' : ''}
          <span className="text-sub">{q.trim()}</span>
        </>
      ),
      run: () => {
        if (window.matchMedia('(min-width: 64rem)').matches) openChat({ send: q.trim() || undefined })
        else {
          navigate('/ask')
          if (q.trim()) openChat({ send: q.trim() })
        }
      },
    })
    for (const p of proposals.data?.items ?? []) {
      if (match(`${p.title} ${p.service} ${p.signal_key}`))
        out.push({
          id: `p:${p.id}`,
          group: 'Proposals',
          label: p.title,
          hint: f.eur(p.savings.eur_month) + '/mo',
          run: () => navigate(`/proposals/${p.id}`),
        })
    }
    for (const s of signals.data?.items ?? []) {
      if (needle && match(`${s.service} ${s.key} ${s.kind}`))
        out.push({
          id: `s:${s.id}`,
          group: 'Signals',
          label: (
            <>
              {s.key} <span className="text-soft">{s.service}</span>
            </>
          ),
          hint: f.KIND_TAG[s.kind],
          run: () => navigate(`/signals/${s.id}`),
        })
    }
    const pages = [...MAIN, ...SETUP].filter((n) => match(n.label))
    for (const n of pages) out.push({ id: `n:${n.path}`, group: 'Go to', label: n.label, run: () => navigate(n.path) })
    if (match('run analysis')) out.push({ id: 'run', group: 'Agent', label: 'Run analysis', run: () => startRun('analyze') })
    return out.slice(0, 40)
  }, [q, proposals.data, signals.data, openChat, startRun])

  useEffect(() => {
    if (paletteOpen) window.setTimeout(() => input.current?.focus(), 0)
  }, [paletteOpen])

  if (!paletteOpen) return null
  const pick = (c: Command) => {
    close()
    c.run()
  }
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((a) => Math.min(commands.length - 1, a + 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((a) => Math.max(0, a - 1))
    } else if (e.key === 'Enter' && commands[active]) {
      e.preventDefault()
      pick(commands[active])
    } else if (e.key === 'Escape') close()
  }
  let lastGroup = ''
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-start justify-center px-3 pt-[12vh]">
      <div className="absolute inset-0 bg-ink/20 dark:bg-black/50" onClick={close} aria-hidden />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        className="relative w-full max-w-xl overflow-hidden rounded-lg bg-bg-white shadow-popover"
      >
        <div className="flex items-center gap-2 border-b border-stroke px-3">
          <RiSearchLine className="size-4 shrink-0 text-soft" />
          <input
            ref={input}
            value={q}
            onChange={(e) => {
              setQ(e.target.value)
              setActive(0)
            }}
            onKeyDown={onKey}
            placeholder="Ask Sift or search signals and proposals…"
            aria-label="Search"
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-list"
            aria-activedescendant={commands[active] ? `cmd-${active}` : undefined}
            className="h-12 flex-1 bg-transparent text-ui text-strong outline-none placeholder:text-soft"
          />
          <kbd className="text-ui-sm text-soft">esc</kbd>
        </div>
        <ul id="palette-list" role="listbox" className="max-h-[60vh] overflow-y-auto p-1">
          {commands.map((c, i) => {
            const header = c.group !== lastGroup ? c.group : null
            lastGroup = c.group
            return (
              <li key={c.id} role="presentation">
                {header && <div className="px-2 pt-2 pb-1 text-ui-sm text-soft">{header}</div>}
                <button
                  id={`cmd-${i}`}
                  type="button"
                  role="option"
                  aria-selected={i === active}
                  onMouseMove={() => setActive(i)}
                  onClick={() => pick(c)}
                  className={cn(
                    'flex min-h-10 w-full items-center gap-2 rounded-md px-2 text-left text-ui text-strong outline-none lg:min-h-8',
                    i === active && 'bg-bg-weak',
                  )}
                >
                  {c.id === 'ask' && <AgentMark size="sm" />}
                  <span className="min-w-0 flex-1 truncate">{c.label}</span>
                  {c.hint && <span className="shrink-0 text-ui-sm text-soft">{c.hint}</span>}
                </button>
              </li>
            )
          })}
        </ul>
      </div>
    </div>,
    document.body,
  )
}

// --- menu page (mobile) -----------------------------------------------------------------

export function MenuList() {
  const { dark, setTheme, meta } = useApp()
  const items = [...MAIN.slice(2), ...SETUP]
  return (
    <div className="space-y-5">
      <div className="overflow-hidden rounded-2xl bg-bg-white">
        {items.map((n, i) => {
          const Icon = n.icon
          return (
            <a
              key={n.path}
              href={href(n.path)}
              className={cn('flex min-h-14 items-center gap-3 px-4 text-ui text-strong', i > 0 && 'border-t border-stroke')}
            >
              <Icon className="size-5 text-soft" />
              <span className="flex-1">{n.label}</span>
              <RiArrowRightSLine className="size-5 text-soft" />
            </a>
          )
        })}
      </div>
      <div className="overflow-hidden rounded-2xl bg-bg-white">
        <button
          type="button"
          onClick={() => setTheme(dark ? 'light' : 'dark')}
          className="flex min-h-14 w-full items-center gap-3 px-4 text-left text-ui text-strong"
        >
          {dark ? <RiSunLine className="size-5 text-soft" /> : <RiMoonLine className="size-5 text-soft" />}
          <span className="flex-1">{dark ? 'Light mode' : 'Dark mode'}</span>
        </button>
        <a href={href('/settings/profile')} className="flex min-h-14 items-center gap-3 border-t border-stroke px-4">
          <Avatar name={meta?.user.name ?? '?'} />
          <span className="flex-1">
            <span className="block text-ui text-strong">{meta?.user.name}</span>
            <span className="block text-ui-sm text-soft">{meta?.user.role}</span>
          </span>
          <RiArrowRightSLine className="size-5 text-soft" />
        </a>
      </div>
      {meta && (
        <p className="px-1 text-ui-sm text-soft">
          Sift {meta.version} · <Tag className="align-middle">{meta.mode}</Tag> · apply target {meta.apply_target}
        </p>
      )}
    </div>
  )
}

// --- shell ------------------------------------------------------------------------------

function Toasts() {
  const { toasts } = useApp()
  if (!toasts.length) return null
  return createPortal(
    <div className="pointer-events-none fixed inset-x-0 bottom-36 z-[60] flex flex-col items-center gap-2 px-4 lg:bottom-6">
      {toasts.map((t) => (
        <div
          key={t.id}
          role="status"
          className="pointer-events-auto max-w-md rounded-lg bg-bg-strong px-3.5 py-2.5 text-ui text-bg-white shadow-popover"
        >
          {t.text}
        </div>
      ))}
    </div>,
    document.body,
  )
}

export function Shell({ children }: { children: ReactNode }) {
  const { setPaletteOpen, paletteOpen } = useApp()
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen(!paletteOpen)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [paletteOpen, setPaletteOpen])

  return (
    <div className="flex min-h-full bg-bg-canvas lg:bg-bg-app">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <MobileHeader />
        <main className="min-w-0 flex-1 pb-24 lg:pb-0">{children}</main>
      </div>
      <TabBar />
      <CommandPalette />
      <Toasts />
    </div>
  )
}
