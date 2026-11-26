# Sift — design rules

The visual language is a calm, dense, professional B2B interface. **Not** an "AI-generated dashboard". Monochrome plus one accent, hairlines instead of
cards, whitespace instead of boxes. Light and dark mode. Desktop first, fully usable on a phone
(approving a proposal from the phone is part of the demo).

Stack: **React 19 + Vite + TypeScript + Tailwind CSS v4** (`@tailwindcss/vite`),
`tailwind-variants`, `tailwind-merge`, `clsx`, icons `@remixicon/react` (line icons only), font
**Inter**. No component library (no shadcn, MUI or Chakra). No chart library: charts are small
hand-written SVG.

## Don't (typical AI slop)
- No emoji in the UI. No "Hello 👋". No sparkles icon for "AI".
- No blur blobs, gradients or glow. No fancy shadowed buttons.
- No coloured square with an icon next to every card or stat.
- No `rounded-2xl` card with a shadow around every block. Don't wrap everything in cards.
- No rainbow avatars, no filled colour pills for every state, no floating action button.
- No icon where text is enough.
- The agent is not a mascot. No avatar, no "thinking" animation beyond a small spinner and a
  text label of the current step.

## Do
- **Monochrome + 1 accent.** Text is `text-strong` / `text-sub` / `text-soft`. Hairlines are
  `border-stroke`. The accent (`primary`) is used only for:
  - the primary action,
  - links,
  - focus,
  - the active nav item,
  - table selection.
- **State = dot + text** (`<Status tone="success">Verified</Status>`). Filled colour only for
  errors, and only rarely.
- **Radius**: buttons and inputs 6 px (`rounded-md`), containers 8 px (`rounded-lg`). Nothing
  larger, except the bottom sheet on mobile.
- **No shadows.** Shadows only on popover, dialog, drawer and command palette (`shadow-popover`).
- **Typography** (Inter, tabular numbers):
  - page title: `text-[26px] font-semibold tracking-[-0.025em]` (mobile `text-[28px] font-bold`)
  - section title: `text-ui-lg font-semibold`
  - body / UI: `text-ui` (13 px desktop, 15 px mobile); descriptions `text-ui text-sub`
  - meta / label: `text-ui-sm text-soft`
  - KPI numbers: `text-[22px] font-semibold tabular-nums tracking-[-0.02em]`. No card: they sit
    in a strip with dividers.
  - code / rules / log samples: `font-mono text-[12px]` on `bg-bg-weak`, `rounded-md`,
    `border border-stroke`
- **Density** (desktop): buttons and inputs 32 px high (`h-8`), table rows about 44 px, page
  padding `px-6 lg:px-10`, max width 1120 px (1440 px for wide tables).
- **Tables**: no wrapping card, full width.
  - header `text-ui-sm text-soft font-medium` with `border-b`,
  - rows `border-b border-stroke`, hover `bg-bg-weak`,
  - numbers right-aligned with `tabular-nums`.
- **Page navigation**: underlined tabs (`<Tabs>`) under the title, not segmented pills.
- **Filters**: a row of small outline buttons ("Kind: All ▾", "Service ▾", "Usage: Unused ▾"),
  search on the left.
- **Empty space** instead of frames. Separate sections with a heading and spacing, not a card.
- **Mobile (< lg)**: grey canvas (`bg-bg-canvas`) with white cards (`Card`/`Group`,
  `rounded-2xl`), large title, iOS-style inset grouped lists, flat tab bar with 3–4 equal items.
  - touch targets ≥ 44 px, list rows ≥ 56 px,
  - one large full-width primary action within thumb reach,
  - never text smaller than 12 px.

## Agent-first UI rules (Sift-specific)
- **The agent's output is a proposal, not a chat bubble.** Anything the agent wants to change
  becomes a proposal row with Reject / Approve. Chat is for questions and instructions.
- **Show work, briefly.** A running agent shows one status line under the page header:
  - a dot, the current step in plain words, and elapsed time,
    e.g. `● Reading 30 days of query history · 0:42`,
  - a "Details" link that opens the run timeline.
  Tool calls in the timeline are single rows: `Queried Dynatrace (DQL) · 1.2 s · 18 rows`.
- **Evidence before verdict.** A proposal detail shows numbers and sources first
  (volume, cost, references, last queried), then the agent's explanation in 3–5 plain sentences,
  then the rule. Every claim links to its source (Dynatrace deep link, query, dashboard).
- **Money is always visible**: savings in €/month and GiB/month on every proposal row, and the
  Gemini usage of each run in the run details.
- **⌘K everywhere**: the command palette searches signals and proposals and has
  "Ask Sift: <text>" as the first item. On mobile, "Ask" is a tab bar item.
- **Approve is deliberate.** The primary button says what happens ("Apply rule"), and the
  confirmation dialog repeats the blast radius ("Used by 0 dashboards, 0 alerts. Revert is
  automatic if verification fails.").

## Shell
- **Sidebar** (desktop): `bg-bg-sidebar`, `border-r border-stroke`, width about 240 px.
  - Top: product mark (accent rounded square `rounded-[24%]` with a white line icon) + "Sift" +
    the Dynatrace environment id in `text-soft`, with `RiExpandUpDownLine` (switch environment,
    popover).
  - Under it: search / command field "Ask Sift or search… ⌘K".
  - Nav: **Overview**, **Proposals** (count in `text-soft` on the right), **Signals**,
    **Activity**.
  - Group label "Setup" (`text-ui-sm text-soft`): **Connections**, **Guardrails**, **Settings**.
  - Bottom: avatar + name, notifications bell, theme toggle (moon/sun).
  - Active item: `bg-bg-soft` (dark: `bg-bg-surface`) `text-strong` with no accent bar. Icons
    are `text-soft`, 16 px.
- **Mobile**: header with mark + name (tap opens the environment bottom sheet). Tab bar:
  Overview · Proposals · Ask · Menu. Icons 24 px, active item in the accent colour.

## Screens

### Overview (first screen)
- `PageHeader`:
  - title "Overview", subtitle "Thursday 26 November 2026 · env abc12345",
  - actions: secondary "Ask Sift", primary "Run analysis".
- Agent status line (dot + text, `text-ui`):
  - left: `● Agent idle · last run 12 min ago` and a thin `Progress` when running,
  - right in `text-sub`: "Next run 14:00", "Verify window 30 min", ghost button "Pause agent".
- `KpiStrip` with 4 KPIs, each with a hint:
  - **Ingest (7 d)** "412 GiB", hint "+8 % vs previous week"
  - **Est. cost / month** "€3,940", hint "at your rates"
  - **Waste found** "38 %", hint "157 GiB in 24 proposals"
  - **Saved so far** "€1,120 / month", hint "9 rules verified"
- Two columns (`lg:grid-cols-[1.5fr_1fr] gap-16`):
  - Left, `Section` "Awaiting approval 3" with "View all →":
    - rows: kind `Tag` (LOG, SPAN, METRIC), title ("Drop DEBUG logs from payment-service"),
      meta in `text-sub` ("−41 GiB/wk · €420/mo · used by nothing in 30 d"),
    - on the right: ghost "Reject", secondary "Approve".
  - Right, `Section` "Ingest by signal":
    - "412 GiB this week", composition bar (Logs `info`, Spans `feature`, Metrics `teal`,
      Events `away`, Other `stroke-sub`),
    - legend rows: dot, name, GiB on the right,
    - last row with a red dot: "2 rules failed verification · Review →".
- Footer line in `text-soft`: "Analyzed 1,284 signals · 30 days of query history · Gemini usage
  today 182k tokens".

### Proposals
- Tabs: Awaiting · Applied · Verified · Reverted · Rejected (counts in `text-soft`).
- Table columns: Proposal, Kind, Service, Savings / mo (right), Confidence, Status, Proposed.
- **Detail** (own URL `#/proposals/:id`, back link):
  - header actions: ghost "Reject", primary "Apply rule";
  - subtitle "Proposed by agent · 12:04 · confidence high";
  - tabs:
    - **Evidence**: KPI strip (Volume/day, Cost/mo, Last queried, References). Then the volume
      chart (14-day bars, one series, a dashed vertical line where a rule was applied). Then
      "Why it's safe": 3–5 sentences with numbered evidence links. Then sample records
      (monospace, redacted).
    - **Rule**: target switch `Segmented` (OpenPipeline / OTel Collector), code block with Copy,
      and a dry-run line ("Matcher hits 1.21 M of 1.24 M records/day (97.6 %)").
    - **Impact**: table of everything that references this data.
    - **History**: audit timeline.

### Signals
- Filter row: search, "Kind ▾", "Service ▾", "Usage ▾" (Unused / Rarely / Used), "Sort: Cost ▾".
- Table: Signal (key in `font-medium`, service under it in `text-soft`), Kind `Tag`,
  Volume/day, Cost/mo, Last used, Used by, Status.
- Detail: same structure as a proposal's Evidence tab, plus "Ask Sift about this signal".

### Activity
- An audit log: time, actor (`Avatar` for people, the neutral mark for the
  agent), action sentence, and "before → after" link. Filter by actor and type.

### Run timeline (drawer from the right, `shadow-popover`)
- Steps with dot + text + duration, tool-call rows collapsed, final summary, token usage and
  estimated Gemini cost of the run.

### Ask Sift (chat drawer / mobile tab)
- Plain messages: the user's right-aligned in `bg-bg-weak rounded-lg`, the agent's left-aligned
  with no bubble.
- Tool calls appear as single muted rows. Proposals the agent creates render as proposal rows
  with Approve.
- Streaming text. Suggested prompts are shown as outline buttons only when the chat is empty.

### Connections, Guardrails, Settings
- List → detail pattern. A setting row has the name and a one-sentence description on the left
  (what it causes, not what it is) and the control on the right.
- Save bar appears only with unsaved changes. Guardrails list protected patterns with a reason.

## Status tones
| Meaning | Tone |
|---|---|
| draft, rejected, protected | `gray` |
| awaiting approval, unused signal | `away` |
| applied / verifying | `feature` |
| verified, used signal | `success` |
| failed verification / reverted after failure | `error` |
| informational | `info` |

Colour never carries meaning alone. There is always a text label next to it.

## Charts
- **Composition bar**: 10 px high, 2 px gaps between segments, only the outer ends rounded,
  value on the right, tooltip per segment.
- **Bars over time**: one series, `fill-current text-stroke-sub`, highlighted bars in the accent,
  no gridlines except a baseline, axis labels `text-ui-sm text-soft`.
- At most one small chart per section, and it must answer a question.

## Components (`web/src/ui.tsx`, build these first)
`Button` (primary | secondary | ghost | danger; size sm | md | lg | icon), `Status`, `Tag`, `Card`,
`Input`, `Select`, `Tabs`, `Segmented`, `Avatar`, `Switch`, `Progress`, `PageHeader`, `Kpi` /
`KpiStrip`, `Section`, `Group`, `Row`, `Page`.
New for Sift: `CodeBlock`, `CompositionBar`, `BarChart`, `Drawer`, `Dialog`, `CommandPalette`,
`ProposalRow`, `RunStatusLine`, `ToolCallRow`, `EmptyState`.

Key class recipes:
- Button base:
  `inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-xl font-medium
  transition-colors focus-visible:ring-2 focus-visible:ring-primary/40 disabled:opacity-40
  lg:gap-1.5 lg:rounded-md [&_svg]:size-5 lg:[&_svg]:size-4`
  - primary `bg-primary text-white hover:bg-primary-dark`
  - secondary `border border-stroke bg-bg-white text-strong hover:bg-bg-weak`
  - ghost `text-sub hover:bg-bg-weak hover:text-strong`
  - danger `border border-stroke bg-bg-white text-error hover:bg-error-lighter`
  - md `h-11 px-4 text-ui lg:h-8 lg:px-3`, sm `h-9 px-3 text-ui-sm lg:h-7 lg:px-2`
- Status: `inline-flex items-center gap-1.5` + dot `size-1.5 rounded-full bg-<tone>` + text
  `text-strong`
- Tag: `h-5 rounded border border-stroke px-1.5 text-[11px] font-medium text-sub`
- KpiStrip (desktop): `flex divide-x divide-stroke border-y border-stroke`. Kpi:
  `px-6 py-3 first:pl-0`.
- Tabs: `flex gap-5 border-b border-stroke`, item `h-10 -mb-px border-b-2`, active
  `border-strong text-strong`, inactive `border-transparent text-sub`.
- Group: `divide-y divide-stroke rounded-lg border border-stroke` (mobile `rounded-2xl bg-bg-white`).
- `cn()` = `tailwind-merge` extended so it knows the custom `text-ui`, `text-ui-sm` and
  `text-ui-lg` sizes (otherwise it merges them with text colours).
- Use only token classes (`bg-bg-white`, `text-sub`, `border-stroke`, `bg-primary`,
  `text-success-dark`…), **never hex values in components**.

## Tokens (`web/src/index.css`)

```css
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@100..900&display=swap');
@import 'tailwindcss';
@custom-variant dark (&:where(.dark, .dark *));

:root {
  --primary-darkest: #164e63; --primary-dark: #155e75; --primary-base: #0e7490;
  --primary-light: #a5f3fc; --primary-lighter: #ecfeff; --primary-focus: rgb(14 116 144 / 16%);
  --bg-strong: #171717; --bg-surface: #262626; --bg-soft: #ebebeb; --bg-weak: #f7f7f7;
  --bg-white: #ffffff; --bg-app: #ffffff; --bg-sidebar: #fafafa; --bg-canvas: #f2f3f5;
  --text-strong: #171717; --text-sub: #525252; --text-soft: #a3a3a3; --text-disabled: #d2d6da;
  --stroke-strong: #171717; --stroke-sub: #d2d6da; --stroke-soft: #ececec;
  --success-dark: #0b4627; --success-base: #1fc16b; --success-light: #c2f5da; --success-lighter: #effaf3;
  --warning-dark: #682f12; --warning-base: #ff8447; --warning-light: #ffd5c0; --warning-lighter: #fff1eb;
  --error-dark: #681219;   --error-base: #fb3748;   --error-light: #ffc0c5;   --error-lighter: #fff1f2;
  --away-dark: #624c18;    --away-base: #f6b51e;    --away-light: #ffecc0;    --away-lighter: #fff9eb;
  --feature-dark: #351a75; --feature-base: #7d52f4; --feature-light: #cac0ff; --feature-lighter: #f4f1ff;
  --info-dark: #122368;    --info-base: #335cff;    --info-light: #c0d5ff;    --info-lighter: #eef4ff;
  --teal-dark: #0b463e;    --teal-base: #22d3bb;    --teal-light: #c2f5ef;    --teal-lighter: #effaf8;
  --gray-dark: #262626;    --gray-base: #737373;    --gray-light: #ebebeb;    --gray-lighter: #f5f5f5;
  --ink: #171717;
  --shadow-input: 0 0 0 1px var(--stroke-soft);
  --shadow-popover: 0 20px 20px -10px rgba(23,23,23,.08), 0 10px 10px -5px rgba(23,23,23,.06), 0 0 0 1px rgba(23,23,23,.08);
  color-scheme: light;
}
.dark {
  --primary-darkest: #cffafe; --primary-dark: #67d3ec; --primary-base: #1597b8;
  --primary-light: #123c4a; --primary-lighter: #0c2630; --primary-focus: rgb(21 151 184 / 28%);
  --bg-strong: #ffffff; --bg-surface: #2a2d33; --bg-soft: #2a2d33; --bg-weak: #1a1b1e;
  --bg-white: #111214; --bg-app: #111214; --bg-sidebar: #0c0d0e; --bg-canvas: #09090a;
  --text-strong: #f5f5f5; --text-sub: #a3a3a3; --text-soft: #737373; --text-disabled: #3f3f46;
  --stroke-strong: #f5f5f5; --stroke-sub: #3f3f46; --stroke-soft: #232428;
  --success-lighter: #0d2a1b; --success-light: #134a2e; --success-dark: #8ef0bb;
  --warning-lighter: #2e1a10; --warning-light: #5a2f17; --warning-dark: #ffbf9e;
  --error-lighter: #2e1014;   --error-light: #5c1a22;   --error-dark: #ff9aa4;
  --away-lighter: #2b2310;    --away-light: #4f3e14;    --away-dark: #ffd98a;
  --feature-lighter: #1e1636; --feature-light: #362666; --feature-dark: #c4b2ff;
  --info-lighter: #121a36;    --info-light: #1e2c66;    --info-dark: #a8bcff;
  --teal-lighter: #0c2925;    --teal-light: #12483f;    --teal-dark: #8ef0e2;
  --gray-lighter: #1f2125;    --gray-light: #2e3036;    --gray-dark: #d4d4d4;
  --shadow-input: 0 0 0 1px var(--stroke-soft);
  --shadow-popover: 0 20px 40px -10px rgba(0,0,0,.6), 0 0 0 1px rgba(255,255,255,.08);
  color-scheme: dark;
}

@theme inline {
  --font-sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  --color-primary-darkest: var(--primary-darkest); --color-primary-dark: var(--primary-dark);
  --color-primary: var(--primary-base); --color-primary-light: var(--primary-light);
  --color-primary-lighter: var(--primary-lighter);
  --color-bg-strong: var(--bg-strong); --color-bg-surface: var(--bg-surface); --color-bg-soft: var(--bg-soft);
  --color-bg-weak: var(--bg-weak); --color-bg-white: var(--bg-white); --color-bg-app: var(--bg-app);
  --color-bg-sidebar: var(--bg-sidebar); --color-bg-canvas: var(--bg-canvas);
  --color-strong: var(--text-strong); --color-sub: var(--text-sub); --color-soft: var(--text-soft);
  --color-disabled: var(--text-disabled);
  --color-stroke-strong: var(--stroke-strong); --color-stroke-sub: var(--stroke-sub); --color-stroke: var(--stroke-soft);
  /* for each tone T in success, warning, error, away, feature, info, teal, gray:
     --color-T: var(--T-base); --color-T-dark: var(--T-dark); --color-T-light: var(--T-light); --color-T-lighter: var(--T-lighter); */
  --color-ink: var(--ink);
  --shadow-input: var(--shadow-input); --shadow-popover: var(--shadow-popover);
}

@layer base {
  html, body, #root { height: 100%; }
  body {
    @apply bg-bg-app font-sans text-strong antialiased;
    font-feature-settings: 'cv11', 'ss01';
    font-size: 15px;
    @media (width >= 64rem) { font-size: 13px; }
  }
  button { cursor: pointer; }
}

/* responsive type: larger on mobile, denser on desktop (lg) */
@utility text-ui    { font-size: 15px; line-height: 22px; @media (width >= 64rem) { font-size: 13px; line-height: 20px; } }
@utility text-ui-sm { font-size: 13px; line-height: 18px; @media (width >= 64rem) { font-size: 12px; line-height: 16px; } }
@utility text-ui-lg { font-size: 17px; line-height: 24px; @media (width >= 64rem) { font-size: 15px; line-height: 22px; } }
```
