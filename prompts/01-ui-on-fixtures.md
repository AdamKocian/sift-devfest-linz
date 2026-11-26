# Phase 1 — The whole UI on fixture data

Read `AGENTS.md`, `DESIGN.md`, `docs/DEMO_WORLD.md` and `docs/PLAN.md` first.
`DESIGN.md` is the spec. When in doubt, choose the calmer, denser, more monochrome option.

## Build `web/`
1. `src/index.css` with the tokens exactly as in DESIGN.md, plus light/dark toggle (class `dark`
   on `<html>`, remembered in localStorage, default = system).
2. `src/ui.tsx` with every component listed in DESIGN.md, using the class recipes given there.
   Then the shell: sidebar, environment switcher popover, ⌘K command palette, mobile header,
   tab bar, bottom sheet.
3. Typed API client `src/api.ts` with the endpoints from AGENTS.md. For now it is backed by a
   **mock adapter** that serves fixture data generated from `docs/DEMO_WORLD.md`:
   - signals with realistic volumes,
   - 8 proposals across all statuses,
   - an activity log,
   - one finished agent run with steps and tool calls,
   - one scripted chat conversation.
   Use the same TypeScript types the server will return, in `src/types.ts`.
4. Screens from DESIGN.md:
   - Overview,
   - Proposals (list + detail with Evidence / Rule / Impact / History),
   - Signals (list + detail),
   - Activity,
   - Run timeline drawer,
   - Ask Sift chat drawer (streams the scripted conversation token by token),
   - Connections, Guardrails, Settings (list → detail pattern).
   Routing with simple hash routes (`#/proposals/:id`); no router library unless needed.
5. Approve flow: "Apply rule" → confirmation dialog that repeats the blast radius → the status
   changes to `applied` → after a fake verification delay → `verified`. The Activity log records
   each step.
6. Responsive: everything works at 375 px. Proposals can be approved on a phone.

## Acceptance (run these, show output)
- `npm run build` and `npm run lint` pass with no errors.
- If you can run a headless browser (Playwright), save screenshots to `docs/screenshots/`:
  - Overview, Proposal detail and Signals in light and dark at 1440×900,
  - Overview and Proposal detail at 375×812.
  If you cannot run one, say so, and list exactly what I should check by hand.
- Self-review against the "Don't" list in DESIGN.md. List every violation you found and fixed.
- No hex colours in components: show the output of `grep -rn "#[0-9a-fA-F]\{3,6\}" web/src --include=*.tsx`.

Update `docs/PLAN.md` and stop with a short summary.
