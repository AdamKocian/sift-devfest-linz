// Sift UI components. Recipes from DESIGN.md: monochrome + one accent, hairlines, no cards on
// desktop, white cards on the grey canvas on mobile. Token classes only, never hex colours.
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react'
import { createPortal } from 'react-dom'
import { clsx, type ClassValue } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'
import { tv, type VariantProps } from 'tailwind-variants'
import {
  RiArrowDownSLine,
  RiArrowLeftSLine,
  RiArrowRightSLine,
  RiCheckLine,
  RiCloseLine,
  RiFileCopyLine,
  RiFilter3Line,
} from '@remixicon/react'
import { count, shortDay } from './format'
import type { Marker, SeriesPoint } from './types'

// tailwind-merge must know the custom text-ui sizes, otherwise it merges them with colours.
const twMerge = extendTailwindMerge({
  extend: { classGroups: { 'font-size': [{ text: ['ui', 'ui-sm', 'ui-lg'] }] } },
})

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}

// --- Button -----------------------------------------------------------------------------

export const button = tv({
  base:
    'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-xl font-medium outline-none ' +
    'transition-colors focus-visible:ring-2 focus-visible:ring-primary/40 disabled:pointer-events-none ' +
    'disabled:opacity-40 lg:gap-1.5 lg:rounded-md [&_svg]:size-5 [&_svg]:shrink-0 lg:[&_svg]:size-4',
  variants: {
    variant: {
      primary: 'bg-primary text-white hover:bg-primary-dark',
      secondary: 'border border-stroke bg-bg-white text-strong hover:bg-bg-weak',
      ghost: 'text-sub hover:bg-bg-weak hover:text-strong',
      danger: 'border border-stroke bg-bg-white text-error hover:bg-error-lighter',
    },
    size: {
      sm: 'h-9 px-3 text-ui-sm lg:h-7 lg:px-2',
      md: 'h-11 px-4 text-ui lg:h-8 lg:px-3',
      lg: 'h-12 px-5 text-ui-lg lg:h-9 lg:px-4 lg:text-ui',
      icon: 'size-11 lg:size-8',
    },
  },
  defaultVariants: { variant: 'secondary', size: 'md' },
})

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof button>

export function Button({ variant, size, className, type = 'button', ...props }: ButtonProps) {
  return <button type={type} className={cn(button({ variant, size }), className)} {...props} />
}

export function LinkButton({
  href,
  variant,
  size,
  className,
  children,
}: { href: string; className?: string; children: ReactNode } & VariantProps<typeof button>) {
  return (
    <a href={href} className={cn(button({ variant, size }), className)}>
      {children}
    </a>
  )
}

/** Inline text link in the accent colour. */
export function TextLink({
  href,
  onClick,
  className,
  children,
}: {
  href?: string
  onClick?: () => void
  className?: string
  children: ReactNode
}) {
  const cls = cn(
    'rounded-sm text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary/40',
    className,
  )
  if (href) {
    return (
      <a href={href} className={cls}>
        {children}
      </a>
    )
  }
  return (
    <button type="button" onClick={onClick} className={cls}>
      {children}
    </button>
  )
}

// --- Status, Tag, Avatar, marks ---------------------------------------------------------

export type Tone = 'gray' | 'away' | 'feature' | 'success' | 'error' | 'info' | 'teal' | 'warning'

const DOT: Record<Tone, string> = {
  gray: 'bg-gray',
  away: 'bg-away',
  feature: 'bg-feature',
  success: 'bg-success',
  error: 'bg-error',
  info: 'bg-info',
  teal: 'bg-teal',
  warning: 'bg-warning',
}

export function Dot({ tone, className }: { tone: Tone; className?: string }) {
  return <span aria-hidden className={cn('inline-block size-1.5 shrink-0 rounded-full', DOT[tone], className)} />
}

/** State = dot + text. Colour never carries meaning alone. */
export function Status({ tone, children, className }: { tone: Tone; children: ReactNode; className?: string }) {
  return (
    <span className={cn('inline-flex items-center gap-1.5 whitespace-nowrap text-strong', className)}>
      <Dot tone={tone} />
      <span>{children}</span>
    </span>
  )
}

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        'inline-block size-3 shrink-0 animate-spin rounded-full border-[1.5px] border-stroke-sub border-t-strong',
        className,
      )}
    />
  )
}

export function Tag({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex h-5 shrink-0 items-center rounded border border-stroke px-1.5 text-[11px] font-medium uppercase tracking-[0.02em] text-sub',
        className,
      )}
    >
      {children}
    </span>
  )
}

export function Avatar({ name, size = 'md', className }: { name: string; size?: 'sm' | 'md'; className?: string }) {
  const initials = name
    .split(/\s+/)
    .map((p) => p[0])
    .filter(Boolean)
    .slice(0, 2)
    .join('')
    .toUpperCase()
  return (
    <span
      aria-hidden
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded-full bg-bg-soft font-medium text-sub',
        size === 'sm' ? 'size-5 text-[10px]' : 'size-7 text-[11px]',
        className,
      )}
    >
      {initials}
    </span>
  )
}

/** Product mark: accent rounded square with a white line icon. */
export function Mark({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        'inline-flex size-7 shrink-0 items-center justify-center rounded-[24%] bg-primary text-white [&_svg]:size-4',
        className,
      )}
    >
      <RiFilter3Line />
    </span>
  )
}

/** The agent in lists: the neutral (monochrome) version of the mark. Not a mascot. */
export function AgentMark({ size = 'md', className }: { size?: 'sm' | 'md'; className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded-[24%] bg-bg-strong text-bg-white',
        size === 'sm' ? 'size-5 [&_svg]:size-3' : 'size-7 [&_svg]:size-4',
        className,
      )}
    >
      <RiFilter3Line />
    </span>
  )
}

// --- form controls ----------------------------------------------------------------------

const field =
  'w-full rounded-xl border border-stroke bg-bg-white px-3 text-ui text-strong outline-none transition-colors ' +
  'placeholder:text-soft focus-visible:border-stroke-sub focus-visible:ring-2 focus-visible:ring-primary/40 ' +
  'disabled:opacity-50 lg:rounded-md'

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn(field, 'h-11 lg:h-8', className)} {...props} />
}

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(field, 'min-h-20 py-2 leading-normal', className)} {...props} />
}

export function Select({ className, children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <span className={cn('relative inline-flex w-full', className)}>
      <select className={cn(field, 'h-11 appearance-none pr-8 lg:h-8')} {...props}>
        {children}
      </select>
      <RiArrowDownSLine className="pointer-events-none absolute top-1/2 right-2 size-4 -translate-y-1/2 text-soft" />
    </span>
  )
}

export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  label: string
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'relative inline-flex h-7 w-12 shrink-0 items-center rounded-full outline-none transition-colors',
        'focus-visible:ring-2 focus-visible:ring-primary/40 disabled:opacity-40 lg:h-5 lg:w-9',
        checked ? 'bg-bg-strong' : 'bg-stroke-sub',
      )}
    >
      <span
        className={cn(
          'inline-block size-6 rounded-full bg-bg-white transition-transform lg:size-4',
          checked ? 'translate-x-[22px] lg:translate-x-[18px]' : 'translate-x-0.5',
        )}
      />
    </button>
  )
}

export function Progress({ value, className }: { value: number; className?: string }) {
  const pct = Math.max(0, Math.min(1, value)) * 100
  return (
    <div
      role="progressbar"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      className={cn('h-0.5 w-full overflow-hidden rounded-full bg-bg-soft', className)}
    >
      <div className="h-full rounded-full bg-strong transition-[width] duration-500" style={{ width: `${pct}%` }} />
    </div>
  )
}

// --- navigation -------------------------------------------------------------------------

export interface TabItem<T extends string> {
  value: T
  label: string
  count?: number
}

/** Underlined page tabs under the title. */
export function Tabs<T extends string>({
  items,
  value,
  onChange,
  className,
}: {
  items: TabItem<T>[]
  value: T
  onChange: (v: T) => void
  className?: string
}) {
  return (
    <div
      role="tablist"
      className={cn(
        'flex gap-5 overflow-x-auto border-b border-stroke [scrollbar-width:none] [&::-webkit-scrollbar]:hidden',
        className,
      )}
    >
      {items.map((it) => {
        const active = it.value === value
        return (
          <button
            key={it.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(it.value)}
            className={cn(
              '-mb-px inline-flex h-11 shrink-0 items-center gap-1.5 border-b-2 text-ui font-medium outline-none transition-colors',
              'focus-visible:text-strong lg:h-10',
              active ? 'border-stroke-strong text-strong' : 'border-transparent text-sub hover:text-strong',
            )}
          >
            {it.label}
            {it.count !== undefined && <span className="font-normal text-soft tabular-nums">{it.count}</span>}
          </button>
        )
      })}
    </div>
  )
}

export function Segmented<T extends string>({
  items,
  value,
  onChange,
  className,
}: {
  items: { value: T; label: string }[]
  value: T
  onChange: (v: T) => void
  className?: string
}) {
  return (
    <div role="radiogroup" className={cn('inline-flex rounded-xl bg-bg-weak p-0.5 ring-1 ring-stroke lg:rounded-md', className)}>
      {items.map((it) => {
        const active = it.value === value
        return (
          <button
            key={it.value}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => onChange(it.value)}
            className={cn(
              'h-9 rounded-[10px] px-3 text-ui-sm font-medium outline-none transition-colors focus-visible:ring-2',
              'focus-visible:ring-primary/40 lg:h-6 lg:rounded-[5px] lg:px-2.5',
              active ? 'bg-bg-white text-strong shadow-input' : 'text-sub hover:text-strong',
            )}
          >
            {it.label}
          </button>
        )
      })}
    </div>
  )
}

// --- layout -----------------------------------------------------------------------------

export function Page({ children, wide, className }: { children: ReactNode; wide?: boolean; className?: string }) {
  return (
    <div
      className={cn(
        'mx-auto w-full px-4 pt-4 pb-10 sm:px-6 lg:px-10 lg:pt-8',
        wide ? 'max-w-[1440px]' : 'max-w-[1120px]',
        className,
      )}
    >
      {children}
    </div>
  )
}

export function PageHeader({
  title,
  subtitle,
  actions,
  back,
  className,
}: {
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  back?: { href: string; label: string }
  className?: string
}) {
  return (
    <header className={cn('mb-5 lg:mb-6', className)}>
      {back && (
        <a
          href={back.href}
          className="-ml-1 mb-2 inline-flex h-9 items-center gap-0.5 rounded-md pr-2 text-ui text-sub outline-none hover:text-strong focus-visible:ring-2 focus-visible:ring-primary/40 lg:h-6"
        >
          <RiArrowLeftSLine className="size-5 lg:size-4" />
          {back.label}
        </a>
      )}
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between lg:gap-6">
        <div className="min-w-0">
          <h1 className="text-[28px] leading-[34px] font-bold tracking-[-0.025em] text-strong lg:text-[26px] lg:leading-8 lg:font-semibold">
            {title}
          </h1>
          {subtitle && <div className="mt-1 text-ui text-sub">{subtitle}</div>}
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </header>
  )
}

export function Section({
  title,
  count,
  action,
  children,
  className,
  id,
}: {
  title: ReactNode
  count?: number
  action?: ReactNode
  children: ReactNode
  className?: string
  id?: string
}) {
  return (
    <section id={id} className={cn('mt-8 lg:mt-10', className)}>
      <div className="mb-3 flex items-baseline justify-between gap-4">
        <h2 className="text-ui-lg font-semibold text-strong">
          {title}
          {count !== undefined && <span className="ml-2 font-normal text-soft tabular-nums">{count}</span>}
        </h2>
        {action && <div className="text-ui">{action}</div>}
      </div>
      {children}
    </section>
  )
}

/** Mobile: white card on the grey canvas. Desktop: no frame at all. */
export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn('rounded-2xl bg-bg-white p-4 lg:rounded-none lg:bg-transparent lg:p-0', className)}>{children}</div>
}

/** Inset grouped list: hairline rows. */
export function Group({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        'divide-y divide-stroke overflow-hidden rounded-2xl bg-bg-white lg:rounded-lg lg:border lg:border-stroke',
        className,
      )}
    >
      {children}
    </div>
  )
}

export function Row({
  title,
  description,
  meta,
  right,
  href,
  onClick,
  selected,
  className,
}: {
  title: ReactNode
  description?: ReactNode
  meta?: ReactNode
  right?: ReactNode
  href?: string
  onClick?: () => void
  selected?: boolean
  className?: string
}) {
  const inner = (
    <>
      <div className="min-w-0 flex-1">
        <div className="truncate text-ui font-medium text-strong">{title}</div>
        {description && <div className="mt-0.5 text-ui-sm text-sub lg:text-ui">{description}</div>}
        {meta && <div className="mt-0.5 text-ui-sm text-soft">{meta}</div>}
      </div>
      {right && <div className="flex shrink-0 items-center gap-2 text-ui text-sub">{right}</div>}
      {(href || onClick) && <RiArrowRightSLine className="size-5 shrink-0 text-soft lg:size-4" />}
    </>
  )
  const cls = cn(
    'flex min-h-14 w-full items-center gap-3 px-4 py-3 text-left outline-none lg:min-h-11 lg:px-3 lg:py-2.5',
    (href || onClick) && 'transition-colors hover:bg-bg-weak focus-visible:bg-bg-weak',
    selected && 'bg-primary-lighter hover:bg-primary-lighter',
    className,
  )
  if (href) {
    return (
      <a href={href} className={cls} aria-current={selected ? 'page' : undefined}>
        {inner}
      </a>
    )
  }
  if (onClick) {
    return (
      <button type="button" onClick={onClick} className={cls}>
        {inner}
      </button>
    )
  }
  return <div className={cls}>{inner}</div>
}

/** A setting: name and what it causes on the left, the control on the right. */
export function SettingRow({
  label,
  description,
  control,
  htmlFor,
}: {
  label: string
  description: string
  control: ReactNode
  htmlFor?: string
}) {
  return (
    <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6 lg:px-3 lg:py-3">
      <div className="min-w-0">
        <label htmlFor={htmlFor} className="text-ui font-medium text-strong">
          {label}
        </label>
        <div className="mt-0.5 text-ui-sm text-sub lg:text-ui">{description}</div>
      </div>
      <div className="flex shrink-0 items-center gap-2 sm:w-56 sm:justify-end">{control}</div>
    </div>
  )
}

// --- KPIs -------------------------------------------------------------------------------

export function KpiStrip({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        'grid grid-cols-2 gap-px overflow-hidden rounded-2xl bg-stroke',
        'lg:flex lg:gap-0 lg:divide-x lg:divide-stroke lg:rounded-none lg:border-y lg:border-stroke lg:bg-transparent',
        className,
      )}
    >
      {children}
    </div>
  )
}

export function Kpi({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="min-w-0 bg-bg-white px-4 py-3 lg:flex-1 lg:bg-transparent lg:px-6 lg:first:pl-0">
      <div className="text-ui-sm text-soft">{label}</div>
      <div className="mt-0.5 truncate text-[22px] leading-7 font-semibold tracking-[-0.02em] text-strong tabular-nums">
        {value}
      </div>
      {hint && <div className="mt-0.5 text-ui-sm text-sub">{hint}</div>}
    </div>
  )
}

// --- code -------------------------------------------------------------------------------

export function CodeBlock({ code, className, copy = true }: { code: string; className?: string; copy?: boolean }) {
  const [copied, setCopied] = useState(false)
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }
  return (
    <div className={cn('relative rounded-md border border-stroke bg-bg-weak', className)}>
      {copy && (
        <Button
          variant="ghost"
          size="sm"
          onClick={onCopy}
          className="absolute top-1.5 right-1.5 bg-bg-weak"
          aria-label="Copy to clipboard"
        >
          {copied ? <RiCheckLine /> : <RiFileCopyLine />}
          {copied ? 'Copied' : 'Copy'}
        </Button>
      )}
      <pre className={cn('overflow-x-auto p-3 font-mono text-[12px] leading-[18px] text-strong', copy && 'pr-24')}>
        <code>{code}</code>
      </pre>
    </div>
  )
}

// --- charts -----------------------------------------------------------------------------

const SEGMENT: Record<string, string> = {
  logs: 'bg-info',
  spans: 'bg-feature',
  metrics: 'bg-teal',
  events: 'bg-away',
  other: 'bg-stroke-sub',
}

/** 10 px bar, 2 px gaps, only the outer ends rounded. */
export function CompositionBar({
  parts,
  format,
}: {
  parts: { key: string; label: string; value: number }[]
  format: (v: number) => string
}) {
  const total = parts.reduce((s, p) => s + p.value, 0) || 1
  return (
    <div className="flex h-2.5 w-full gap-0.5 overflow-hidden rounded-full" role="img" aria-label="Ingest by signal type">
      {parts.map((p) => (
        <div
          key={p.key}
          title={`${p.label}: ${format(p.value)}`}
          className={cn('h-full min-w-[2px]', SEGMENT[p.key] ?? SEGMENT.other)}
          style={{ width: `${(p.value / total) * 100}%` }}
        />
      ))}
    </div>
  )
}

export function SegmentDot({ k }: { k: string }) {
  return <span aria-hidden className={cn('inline-block size-2 shrink-0 rounded-full', SEGMENT[k] ?? SEGMENT.other)} />
}

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)))
    ro.observe(el)
    setWidth(Math.floor(el.getBoundingClientRect().width))
    return () => ro.disconnect()
  }, [])
  return [ref, width]
}

/** Bars over time: one series, baseline only, dashed line where a rule was applied. */
export function BarChart({
  series,
  markers = [],
  format,
  height = 140,
  label,
}: {
  series: SeriesPoint[]
  markers?: Marker[]
  format: (v: number) => string
  height?: number
  label: string
}) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [hover, setHover] = useState<number | null>(null)
  const max = Math.max(...series.map((p) => p.gib), 0.0001)
  const top = 18
  const axis = 18
  const plot = height - top - axis
  const n = series.length
  const slot = n ? width / n : 0
  const gap = Math.max(2, Math.min(6, slot * 0.25))
  const barW = Math.max(1, slot - gap)
  const markerIdx = markers
    .map((m) => ({ ...m, i: series.findIndex((p) => p.date === m.date) }))
    .filter((m) => m.i >= 0)
  const shown = hover ?? n - 1
  const labelEvery = width < 420 ? Math.ceil(n / 4) : Math.ceil(n / 7)

  return (
    <div className="w-full select-none">
      <div className="mb-1 h-4 text-right text-ui-sm text-sub tabular-nums" aria-live="polite">
        {n > 0 && (
          <>
            {shown === n - 1 ? 'Today (projected)' : shortDay(series[shown].date)}: {format(series[shown].gib)}
          </>
        )}
      </div>
      <div ref={ref} className="relative w-full" style={{ height }}>
        {width > 0 && n > 0 && (
          <svg width={width} height={height} role="img" aria-label={label} className="block overflow-visible">
            {series.map((p, i) => {
              const h = Math.max(p.gib > 0 ? 1 : 0, (p.gib / max) * plot)
              const x = i * slot + gap / 2
              const active = hover === i
              return (
                <g key={p.date} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
                  <rect x={i * slot} y={top} width={slot} height={plot} fill="transparent" />
                  <rect
                    x={x}
                    y={top + plot - h}
                    width={barW}
                    height={h}
                    rx={Math.min(2, barW / 3)}
                    className={cn('fill-current transition-colors', active ? 'text-primary' : 'text-stroke-sub')}
                  />
                </g>
              )
            })}
            <line x1={0} x2={width} y1={top + plot + 0.5} y2={top + plot + 0.5} className="stroke-current text-stroke" />
            {markerIdx.map((m) => {
              const x = Math.round(m.i * slot) + 0.5
              const flip = x > width - 110
              return (
                <g key={`${m.proposal_id}-${m.label}`} className="text-sub">
                  <line x1={x} x2={x} y1={top - 6} y2={top + plot} strokeDasharray="3 3" className="stroke-current" />
                  <text
                    x={flip ? x - 4 : x + 4}
                    y={top - 8}
                    textAnchor={flip ? 'end' : 'start'}
                    className="fill-current text-[11px]"
                  >
                    {m.label}
                  </text>
                </g>
              )
            })}
            {series.map((p, i) =>
              i % labelEvery === 0 || i === n - 1 ? (
                <text
                  key={p.date}
                  x={i * slot + slot / 2}
                  y={height - 3}
                  textAnchor="middle"
                  className="fill-current text-[11px] text-soft"
                >
                  {i === n - 1 ? 'Today' : shortDay(p.date)}
                </text>
              ) : null,
            )}
          </svg>
        )}
      </div>
    </div>
  )
}

// --- overlays ---------------------------------------------------------------------------

function useEscape(onClose: () => void, active: boolean) {
  useEffect(() => {
    if (!active) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, active])
}

function useLockScroll(active: boolean) {
  useEffect(() => {
    if (!active) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = prev
    }
  }, [active])
}

/** Focus the first focusable element inside `ref` when it opens. */
function useAutoFocus(ref: React.RefObject<HTMLElement | null>, active: boolean) {
  useEffect(() => {
    if (!active || !ref.current) return
    const el = ref.current.querySelector<HTMLElement>('[data-autofocus], input, textarea, button:not([data-close])')
    el?.focus()
  }, [active, ref])
}

export function Drawer({
  open,
  onClose,
  title,
  actions,
  children,
  footer,
}: {
  open: boolean
  onClose: () => void
  title: ReactNode
  actions?: ReactNode
  children: ReactNode
  footer?: ReactNode
}) {
  useEscape(onClose, open)
  if (!open) return null
  return createPortal(
    <div className="fixed inset-0 z-40">
      <div className="absolute inset-0 bg-ink/20 dark:bg-black/50" onClick={onClose} aria-hidden />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === 'string' ? title : undefined}
        className="absolute inset-y-0 right-0 flex w-full flex-col bg-bg-white shadow-popover sm:max-w-[460px] lg:border-l lg:border-stroke"
      >
        <div className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-stroke px-4 lg:h-12">
          <div className="min-w-0 truncate text-ui-lg font-semibold text-strong">{title}</div>
          <div className="flex items-center gap-1">
            {actions}
            <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close" data-close>
              <RiCloseLine />
            </Button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
        {footer && <div className="shrink-0 border-t border-stroke pb-safe">{footer}</div>}
      </aside>
    </div>,
    document.body,
  )
}

/** Centered dialog on desktop, bottom sheet on mobile. */
export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
}: {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
  footer?: ReactNode
}) {
  const ref = useRef<HTMLDivElement>(null)
  const titleId = useId()
  useEscape(onClose, open)
  useLockScroll(open)
  useAutoFocus(ref, open)
  if (!open) return null
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-end justify-center lg:items-center lg:p-6">
      <div className="absolute inset-0 bg-ink/30 dark:bg-black/60" onClick={onClose} aria-hidden />
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="relative w-full rounded-t-2xl bg-bg-white pb-safe shadow-popover lg:max-w-[460px] lg:rounded-lg"
      >
        <div className="mx-auto mt-2 h-1 w-9 rounded-full bg-stroke-sub lg:hidden" aria-hidden />
        <div className="px-5 pt-4 lg:pt-5">
          <h2 id={titleId} className="text-ui-lg font-semibold text-strong">
            {title}
          </h2>
          <div className="mt-2 text-ui text-sub">{children}</div>
        </div>
        {footer && (
          <div className="mt-5 flex flex-col-reverse gap-2 border-t border-stroke px-5 py-4 lg:flex-row lg:justify-end lg:py-3">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

/** Mobile bottom sheet with an inset list (environment switcher, filters). */
export function BottomSheet({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
}) {
  useEscape(onClose, open)
  useLockScroll(open)
  if (!open) return null
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-end">
      <div className="absolute inset-0 bg-ink/30 dark:bg-black/60" onClick={onClose} aria-hidden />
      <div role="dialog" aria-modal="true" aria-label={title} className="relative w-full rounded-t-2xl bg-bg-canvas pb-safe shadow-popover">
        <div className="mx-auto mt-2 h-1 w-9 rounded-full bg-stroke-sub" aria-hidden />
        <div className="flex items-center justify-between px-4 pt-2 pb-1">
          <div className="text-ui-lg font-semibold text-strong">{title}</div>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close">
            <RiCloseLine />
          </Button>
        </div>
        <div className="max-h-[70vh] overflow-y-auto px-4 pb-4">{children}</div>
      </div>
    </div>,
    document.body,
  )
}

/** Anchored popover. `align` picks the edge of the trigger it lines up with. */
export function Popover({
  trigger,
  children,
  align = 'start',
  side = 'bottom',
  className,
}: {
  trigger: (props: { open: boolean; toggle: () => void }) => ReactNode
  children: (close: () => void) => ReactNode
  align?: 'start' | 'end'
  side?: 'bottom' | 'top'
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const close = useCallback(() => setOpen(false), [])
  useEscape(close, open)
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])
  return (
    <div ref={ref} className="relative inline-flex">
      {trigger({ open, toggle: () => setOpen((o) => !o) })}
      {open && (
        <div
          className={cn(
            'absolute z-30 min-w-48 rounded-lg bg-bg-white p-1 shadow-popover',
            side === 'bottom' ? 'top-full mt-1' : 'bottom-full mb-1',
            align === 'start' ? 'left-0' : 'right-0',
            className,
          )}
        >
          {children(close)}
        </div>
      )}
    </div>
  )
}

export function MenuItem({
  children,
  onClick,
  href,
  selected,
  meta,
}: {
  children: ReactNode
  onClick?: () => void
  href?: string
  selected?: boolean
  meta?: ReactNode
}) {
  const cls =
    'flex min-h-10 w-full items-center gap-2 rounded-md px-2 text-left text-ui text-strong outline-none hover:bg-bg-weak focus-visible:bg-bg-weak lg:min-h-8'
  const inner = (
    <>
      <span className="flex min-w-0 flex-1 items-center gap-2 truncate">{children}</span>
      {meta && <span className="shrink-0 text-ui-sm text-soft">{meta}</span>}
      <RiCheckLine className={cn('size-4 shrink-0', selected ? 'text-strong' : 'invisible')} />
    </>
  )
  if (href) {
    return (
      <a href={href} className={cls}>
        {inner}
      </a>
    )
  }
  return (
    <button type="button" className={cls} onClick={onClick} role="menuitemradio" aria-checked={selected}>
      {inner}
    </button>
  )
}

/** Filter button: "Kind: All ▾" with a menu of options. */
export function FilterMenu<T extends string>({
  label,
  value,
  options,
  onChange,
  align = 'start',
}: {
  label: string
  value: T
  options: { value: T; label: string }[]
  onChange: (v: T) => void
  align?: 'start' | 'end'
}) {
  const current = options.find((o) => o.value === value)?.label ?? value
  return (
    <Popover
      align={align}
      trigger={({ open, toggle }) => (
        <Button size="sm" onClick={toggle} aria-expanded={open} aria-haspopup="menu">
          <span className="text-sub">{label}:</span>
          <span>{current}</span>
          <RiArrowDownSLine className="-mr-0.5 text-soft" />
        </Button>
      )}
    >
      {(close) => (
        <div role="menu" className="max-h-80 overflow-y-auto">
          {options.map((o) => (
            <MenuItem
              key={o.value}
              selected={o.value === value}
              onClick={() => {
                onChange(o.value)
                close()
              }}
            >
              {o.label}
            </MenuItem>
          ))}
        </div>
      )}
    </Popover>
  )
}

// --- small agent pieces -----------------------------------------------------------------

/** A tool call is one muted row: "Queried Dynatrace (DQL) · 1.2 s · 18 rows". */
export function ToolCallRow({
  label,
  duration,
  rows,
  className,
}: {
  label: string
  duration?: string
  rows?: number | null
  className?: string
}) {
  return (
    <div className={cn('flex min-w-0 items-baseline gap-2 text-ui-sm text-sub', className)}>
      <span aria-hidden className="relative top-[-2px] inline-block size-1 shrink-0 rounded-full bg-stroke-sub" />
      <span className="min-w-0">
        {label}
        {duration && <span className="text-soft"> · {duration}</span>}
        {rows != null && (
          <span className="text-soft">
            {' '}
            · {count(rows)} {rows === 1 ? 'row' : 'rows'}
          </span>
        )}
      </span>
    </div>
  )
}

export function EmptyState({
  title,
  description,
  action,
  className,
}: {
  title: string
  description?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('rounded-2xl bg-bg-white px-6 py-12 text-center lg:rounded-none lg:bg-transparent lg:py-16', className)}>
      <div className="text-ui-lg font-semibold text-strong">{title}</div>
      {description && <div className="mx-auto mt-1 max-w-sm text-ui text-sub">{description}</div>}
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </div>
  )
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-16 text-ui text-soft" role="status">
      <Spinner />
      {label}
    </div>
  )
}

export function ErrorNote({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div role="alert" className="my-6 rounded-lg border border-error-light bg-error-lighter px-4 py-3 text-ui text-error-dark">
      {error.message}
      {onRetry && (
        <button type="button" onClick={onRetry} className="ml-2 underline">
          Retry
        </button>
      )}
    </div>
  )
}

/** Table primitives: no wrapping card, hairline rows, numbers right-aligned. */
export function Th({ children, right, className }: { children?: ReactNode; right?: boolean; className?: string }) {
  return (
    <th
      scope="col"
      className={cn(
        'h-9 border-b border-stroke px-3 text-ui-sm font-medium whitespace-nowrap text-soft first:pl-0 last:pr-0',
        right ? 'text-right' : 'text-left',
        className,
      )}
    >
      {children}
    </th>
  )
}

export function Td({ children, right, className }: { children?: ReactNode; right?: boolean; className?: string }) {
  return (
    <td
      className={cn(
        'h-11 border-b border-stroke px-3 py-2 align-middle text-ui first:pl-0 last:pr-0',
        right && 'text-right tabular-nums',
        className,
      )}
    >
      {children}
    </td>
  )
}
