import { useEffect } from 'react'
import { ApproveDialog, ChatDrawer, ChatPanel, RejectDialog, RunDrawer, RunningLine } from './features'
import { ActivityPage } from './pages/Activity'
import { isDesktop } from './pages/common'
import { OverviewPage } from './pages/Overview'
import { ProposalDetailPage, ProposalsPage } from './pages/Proposals'
import { ConnectionsPage, GuardrailsPage, SettingsPage } from './pages/Setup'
import { SignalDetailPage, SignalsPage } from './pages/Signals'
import { navigate, useRoute } from './router'
import { AppProvider, useApp } from './store'
import { MenuList, Shell } from './shell'
import { Button, EmptyState, Page, PageHeader } from './ui'

function AskPage() {
  const { openChat, clearChat, messages } = useApp()
  // On desktop, Ask lives in the drawer.
  useEffect(() => {
    if (isDesktop()) {
      navigate('/')
      window.setTimeout(() => openChat(), 0)
    }
  }, [openChat])
  return (
    <div className="flex h-[calc(100dvh-7rem)] flex-col lg:h-auto">
      <div className="flex items-end justify-between px-4 pt-4">
        <h1 className="text-[28px] leading-[34px] font-bold tracking-[-0.025em] text-strong">Ask Sift</h1>
        {messages.length > 0 && (
          <Button variant="ghost" size="sm" onClick={clearChat}>
            Clear
          </Button>
        )}
      </div>
      <ChatPanel className="min-h-0 flex-1" />
    </div>
  )
}

function MenuPage() {
  return (
    <Page>
      <PageHeader title="Menu" />
      <MenuList />
    </Page>
  )
}

function NotFound() {
  return (
    <Page>
      <EmptyState title="Page not found" description="The link may be out of date." action={<a className="text-primary" href="#/">Go to Overview</a>} />
    </Page>
  )
}

/** One status line under the header while the agent works, on every page but Overview. */
function GlobalRunLine() {
  const { live, openRun } = useApp()
  const route = useRoute()
  if (!live || live.run.status !== 'running' || route.path === '/') return null
  return (
    <div className="border-b border-stroke bg-bg-white px-4 py-2 sm:px-6 lg:bg-bg-app lg:px-10">
      <RunningLine run={live.run} onDetails={() => openRun(live.run.id)} />
    </div>
  )
}

function Routes() {
  const route = useRoute()
  const { closeChat, closeRun } = useApp()
  const [a, b] = route.segments
  useEffect(() => {
    window.scrollTo(0, 0)
    // Following a link out of a drawer closes it.
    closeChat()
    closeRun()
  }, [route.path, closeChat, closeRun])
  switch (a) {
    case undefined:
      return <OverviewPage />
    case 'proposals':
      return b ? <ProposalDetailPage key={b} id={b} /> : <ProposalsPage />
    case 'signals':
      return b ? <SignalDetailPage key={b} id={b} /> : <SignalsPage />
    case 'activity':
      return <ActivityPage />
    case 'connections':
      return <ConnectionsPage id={b} />
    case 'guardrails':
      return <GuardrailsPage id={b} />
    case 'settings':
      return <SettingsPage section={b} />
    case 'ask':
      return <AskPage />
    case 'menu':
      return <MenuPage />
    default:
      return <NotFound />
  }
}

export function App() {
  return (
    <AppProvider>
      <Shell>
        <GlobalRunLine />
        <Routes />
      </Shell>
      <RunDrawer />
      <ChatDrawer />
      <ApproveDialog />
      <RejectDialog />
    </AppProvider>
  )
}
