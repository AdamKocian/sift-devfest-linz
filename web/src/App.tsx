import { RiSparklingLine, RiPulseLine } from '@remixicon/react'
import { Button, Status, Tag } from './ui.tsx'

export function App() {
  return (
    <div className="min-h-screen bg-bg-app text-strong flex flex-col">
      <header className="border-b border-stroke px-6 lg:px-10 h-14 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="size-7 rounded-[24%] bg-primary flex items-center justify-center text-white">
            <RiSparklingLine className="size-4" />
          </div>
          <span className="font-semibold text-ui-lg tracking-tight">Sift</span>
          <span className="text-soft text-ui-sm">env abc12345</span>
        </div>
        <div className="flex items-center gap-2">
          <Status tone="success">Agent idle</Status>
          <Button variant="primary" size="sm">
            <RiPulseLine className="size-3.5" />
            Run analysis
          </Button>
        </div>
      </header>

      <main className="flex-1 px-6 lg:px-10 py-8 max-w-6xl mx-auto w-full">
        <div className="flex flex-col gap-6">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-[26px] font-semibold tracking-[-0.025em] text-strong">Overview</h1>
              <p className="text-ui text-sub">Telemetry optimizer for Dynatrace</p>
            </div>
            <div className="flex items-center gap-2">
              <Tag>Phase 0 Skeleton</Tag>
            </div>
          </div>

          <div className="flex divide-x divide-stroke border-y border-stroke py-3">
            <div className="px-6 first:pl-0">
              <div className="text-ui-sm text-soft">Ingest (7 d)</div>
              <div className="text-[22px] font-semibold tabular-nums tracking-[-0.02em]">412 GiB</div>
            </div>
            <div className="px-6">
              <div className="text-ui-sm text-soft">Est. cost / month</div>
              <div className="text-[22px] font-semibold tabular-nums tracking-[-0.02em]">€3,940</div>
            </div>
            <div className="px-6">
              <div className="text-ui-sm text-soft">Waste found</div>
              <div className="text-[22px] font-semibold tabular-nums tracking-[-0.02em] text-away">38 %</div>
            </div>
            <div className="px-6">
              <div className="text-ui-sm text-soft">Saved so far</div>
              <div className="text-[22px] font-semibold tabular-nums tracking-[-0.02em] text-success">€1,120 / mo</div>
            </div>
          </div>
        </div>
      </main>
    </div>
  )
}

export default App
