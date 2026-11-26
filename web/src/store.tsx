// App-wide state: data invalidation, live agent runs, Ask Sift, dialogs, theme, toasts.
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { api, followRun, streamChat } from './api'
import type { Meta, ProposalSummary, RunEvent, RunSummary, ToolCall } from './types'

// --- data hooks -------------------------------------------------------------------------

type Listener = () => void
const listeners = new Set<Listener>()

/** Every useApi hook refetches. Call after any change. */
export function invalidate(): void {
  for (const l of listeners) l()
}

export interface Resource<T> {
  data: T | undefined
  error: Error | undefined
  loading: boolean
  reload: () => void
}

/**
 * Fetches on mount and whenever `key` changes or data is invalidated.
 * `poll` (ms) refetches in the background, e.g. while a rule is being verified.
 */
export function useApi<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  poll?: number | false | ((data: T | undefined) => number | false),
): Resource<T> {
  const [state, setState] = useState<{ key: string | null; data?: T; error?: Error }>({ key })
  const [tick, setTick] = useState(0)
  const fetchRef = useRef(fetcher)
  useEffect(() => {
    fetchRef.current = fetcher
  })

  const reload = useCallback(() => setTick((t) => t + 1), [])

  useEffect(() => {
    listeners.add(reload)
    return () => {
      listeners.delete(reload)
    }
  }, [reload])

  useEffect(() => {
    if (key === null) return
    let alive = true
    fetchRef
      .current()
      .then((data) => alive && setState({ key, data }))
      .catch((error: Error) => alive && setState((s) => ({ key, data: s.key === key ? s.data : undefined, error })))
    return () => {
      alive = false
    }
  }, [key, tick])

  const current = state.key === key
  const pollMs = typeof poll === 'function' ? poll(current ? state.data : undefined) : poll
  useEffect(() => {
    if (!pollMs || key === null) return
    const id = window.setInterval(reload, pollMs)
    return () => window.clearInterval(id)
  }, [pollMs, key, reload])

  return {
    data: current ? state.data : undefined,
    error: current ? state.error : undefined,
    loading: key !== null && (!current || (state.data === undefined && state.error === undefined)),
    reload,
  }
}

/** Re-render every `ms` (for "2 min ago" and elapsed timers). */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), ms)
    return () => window.clearInterval(id)
  }, [ms])
  return now
}

// --- theme ------------------------------------------------------------------------------

export type ThemePref = 'system' | 'light' | 'dark'
const THEME_KEY = 'sift-theme'

function readTheme(): ThemePref {
  try {
    const v = localStorage.getItem(THEME_KEY)
    return v === 'light' || v === 'dark' ? v : 'system'
  } catch {
    return 'system'
  }
}

function applyTheme(pref: ThemePref): void {
  const dark = pref === 'dark' || (pref === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.toggle('dark', dark)
}

// --- chat -------------------------------------------------------------------------------

export interface ChatContext {
  proposal_id?: string
  signal_id?: string
  label: string
}

export interface ChatMessage {
  id: number
  role: 'user' | 'agent'
  text: string
  tools: ToolCall[]
  proposals: ProposalSummary[]
  suggestions: string[]
  streaming: boolean
  error?: string
}

// --- live runs --------------------------------------------------------------------------

export interface LiveRun {
  run: RunSummary
  events: RunEvent[]
}

// --- context ----------------------------------------------------------------------------

interface Toast {
  id: number
  text: string
}

interface AppState {
  meta: Meta | undefined
  theme: ThemePref
  setTheme: (t: ThemePref) => void
  dark: boolean

  live: LiveRun | null
  startRun: (type: RunSummary['type']) => Promise<void>
  runError: string | null

  runDrawer: string | null | false // run id, null = latest, false = closed
  openRun: (id: string | null) => void
  closeRun: () => void

  chatOpen: boolean
  openChat: (opts?: { context?: ChatContext | null; send?: string }) => void
  closeChat: () => void
  chatContext: ChatContext | null
  setChatContext: (c: ChatContext | null) => void
  messages: ChatMessage[]
  sendChat: (text: string) => void
  clearChat: () => void

  paletteOpen: boolean
  setPaletteOpen: (open: boolean) => void

  approveId: string | null
  setApproveId: (id: string | null) => void
  rejectId: string | null
  setRejectId: (id: string | null) => void

  toasts: Toast[]
  toast: (text: string) => void
}

const Ctx = createContext<AppState | null>(null)

export function useApp(): AppState {
  const v = useContext(Ctx)
  if (!v) throw new Error('useApp outside <AppProvider>')
  return v
}

export function AppProvider({ children }: { children: ReactNode }) {
  const meta = useApi('meta', api.meta, 10000).data

  // theme
  const [theme, setThemeState] = useState<ThemePref>(readTheme)
  const [systemDark, setSystemDark] = useState(() => window.matchMedia('(prefers-color-scheme: dark)').matches)
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => setSystemDark(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
  useEffect(() => applyTheme(theme), [theme, systemDark])
  const setTheme = useCallback((t: ThemePref) => {
    try {
      if (t === 'system') localStorage.removeItem(THEME_KEY)
      else localStorage.setItem(THEME_KEY, t)
    } catch {
      // storage blocked: the choice lasts for this page only
    }
    setThemeState(t)
  }, [])
  const dark = theme === 'dark' || (theme === 'system' && systemDark)

  // toasts
  const [toasts, setToasts] = useState<Toast[]>([])
  const toast = useCallback((text: string) => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, text }])
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4000)
  }, [])

  // live runs: follow whatever run is running (started here, by the scheduler or elsewhere)
  const [live, setLive] = useState<LiveRun | null>(null)
  const [runError, setRunError] = useState<string | null>(null)
  const following = useRef<string | null>(null)

  const follow = useCallback((run: RunSummary) => {
    if (following.current === run.id) return
    following.current = run.id
    setLive({ run, events: [] })
    followRun(run.id, {
      onEvent: (e) => {
        setLive((l) => {
          if (!l || l.run.id !== run.id || l.events.some((x) => x.seq === e.seq)) return l
          const events = [...l.events, e]
          const step = e.type === 'step' ? e.label : l.run.current_step
          const done = l.run.steps_done + (e.type === 'step_done' ? 1 : 0)
          return { run: { ...l.run, current_step: step, steps_done: done }, events }
        })
        if (e.type === 'proposal') invalidate()
      },
      onDone: (final) => {
        following.current = null
        setLive((l) => (l && l.run.id === final.id ? { ...l, run: final } : l))
        invalidate()
      },
      onError: () => {
        following.current = null
      },
    })
  }, [])

  // Discover runs started by the scheduler or another tab.
  const runs = useApi('runs-latest', api.runs, 5000).data
  useEffect(() => {
    const running = runs?.items.find((r) => r.status === 'running')
    if (running) follow(running)
  }, [runs, follow])

  const startRun = useCallback(
    async (type: RunSummary['type']) => {
      setRunError(null)
      try {
        const run = await api.startRun(type)
        follow(run)
        invalidate()
      } catch (e) {
        setRunError((e as Error).message)
        toast((e as Error).message)
      }
    },
    [follow, toast],
  )

  // drawers and dialogs
  const [runDrawer, setRunDrawer] = useState<string | null | false>(false)
  const [chatOpen, setChatOpen] = useState(false)
  const [chatContext, setChatContext] = useState<ChatContext | null>(null)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [approveId, setApproveId] = useState<string | null>(null)
  const [rejectId, setRejectId] = useState<string | null>(null)

  // chat
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const nextId = useRef(1)
  const contextRef = useRef<ChatContext | null>(null)
  useEffect(() => {
    contextRef.current = chatContext
  }, [chatContext])

  const sendChat = useCallback((text: string) => {
    const message = text.trim()
    if (!message) return
    const userId = nextId.current++
    const agentId = nextId.current++
    setMessages((m) => [
      ...m,
      { id: userId, role: 'user', text: message, tools: [], proposals: [], suggestions: [], streaming: false },
      { id: agentId, role: 'agent', text: '', tools: [], proposals: [], suggestions: [], streaming: true },
    ])
    const update = (fn: (m: ChatMessage) => ChatMessage) =>
      setMessages((all) => all.map((m) => (m.id === agentId ? fn(m) : m)))
    const ctx = contextRef.current
    ;(async () => {
      try {
        for await (const ev of streamChat({
          message,
          proposal_id: ctx?.proposal_id,
          signal_id: ctx?.signal_id,
        })) {
          if (ev.type === 'tool') update((m) => ({ ...m, tools: [...m.tools, ev] }))
          else if (ev.type === 'token') update((m) => ({ ...m, text: m.text + ev.text }))
          else if (ev.type === 'proposal') update((m) => ({ ...m, proposals: [...m.proposals, ev.proposal] }))
          else if (ev.type === 'suggestions') update((m) => ({ ...m, suggestions: ev.items }))
        }
        update((m) => ({ ...m, streaming: false }))
      } catch (e) {
        update((m) => ({ ...m, streaming: false, error: (e as Error).message }))
      }
    })()
  }, [])

  const openChat = useCallback(
    (opts?: { context?: ChatContext | null; send?: string }) => {
      if (opts && 'context' in opts) {
        contextRef.current = opts.context ?? null
        setChatContext(opts.context ?? null)
      }
      setChatOpen(true)
      if (opts?.send) sendChat(opts.send)
    },
    [sendChat],
  )

  const openRun = useCallback((id: string | null) => setRunDrawer(id), [])
  const closeRun = useCallback(() => setRunDrawer(false), [])
  const closeChat = useCallback(() => setChatOpen(false), [])

  const value = useMemo<AppState>(
    () => ({
      meta,
      theme,
      setTheme,
      dark,
      live,
      startRun,
      runError,
      runDrawer,
      openRun,
      closeRun,
      chatOpen,
      openChat,
      closeChat,
      chatContext,
      setChatContext,
      messages,
      sendChat,
      clearChat: () => setMessages([]),
      paletteOpen,
      setPaletteOpen,
      approveId,
      setApproveId,
      rejectId,
      setRejectId,
      toasts,
      toast,
    }),
    [
      meta,
      theme,
      setTheme,
      dark,
      live,
      startRun,
      runError,
      runDrawer,
      openRun,
      closeRun,
      chatOpen,
      openChat,
      closeChat,
      chatContext,
      messages,
      sendChat,
      paletteOpen,
      approveId,
      rejectId,
      toasts,
      toast,
    ],
  )

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
