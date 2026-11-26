// Minimal hash router: #/proposals/p-1?tab=rule
import { useSyncExternalStore } from 'react'

export interface Route {
  path: string
  segments: string[]
  query: URLSearchParams
}

function parse(hash: string): Route {
  const raw = hash.replace(/^#/, '') || '/'
  const [path, search = ''] = raw.split('?')
  const clean = path.startsWith('/') ? path : `/${path}`
  return {
    path: clean,
    segments: clean.split('/').filter(Boolean).map(decodeURIComponent),
    query: new URLSearchParams(search),
  }
}

function subscribe(cb: () => void): () => void {
  window.addEventListener('hashchange', cb)
  return () => window.removeEventListener('hashchange', cb)
}

export function useRoute(): Route {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash)
  return parse(hash)
}

export function href(path: string, query?: Record<string, string | undefined | null>): string {
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(query ?? {})) if (v) params.set(k, v)
  const search = params.toString()
  return `#${path}${search ? `?${search}` : ''}`
}

export function navigate(path: string, query?: Record<string, string | undefined | null>): void {
  const next = href(path, query)
  if (window.location.hash !== next) window.location.hash = next
}

/** Replace the query of the current route without adding a history entry. */
export function setQuery(route: Route, patch: Record<string, string | undefined | null>): void {
  const params = new URLSearchParams(route.query)
  for (const [k, v] of Object.entries(patch)) {
    if (v) params.set(k, v)
    else params.delete(k)
  }
  const search = params.toString()
  const url = `#${route.path}${search ? `?${search}` : ''}`
  window.history.replaceState(null, '', url)
  window.dispatchEvent(new HashChangeEvent('hashchange'))
}
