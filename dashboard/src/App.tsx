import { Suspense, lazy, useEffect, useState } from "react"

import {
  MODE_HUMAN,
  TAG_SALES_LEAD,
  UNAUTHORIZED_EVENT,
  api,
  clearApiKey,
  getApiKey,
  setApiKey,
} from "./api"
import { useAsync } from "./components/Async"
import { ToastProvider, useToast } from "./components/Toast"
import {
  EVENT_ACTIVITY,
  EVENT_HANDOFF,
  EventsProvider,
  LiveIndicator,
  useEvents,
} from "./events"
import { flashTitle, playChime, resetTitle } from "./notify"

// Lazily loaded so that recharts, which only the Overview screen needs, stops
// being part of the initial download for an operator who lives in
// Conversations all day.
const Overview = lazy(() => import("./views/Overview"))
const Customers = lazy(() => import("./views/Customers"))
const Conversations = lazy(() => import("./views/Conversations"))
const Search = lazy(() => import("./views/Search"))
const Knowledge = lazy(() => import("./views/Knowledge"))
const Pricing = lazy(() => import("./views/Pricing"))
const Operations = lazy(() => import("./views/Operations"))

type View =
  | "overview"
  | "customers"
  | "conversations"
  | "search"
  | "knowledge"
  | "pricing"
  | "operations"

const NAV: { id: View; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "customers", label: "Customers" },
  { id: "conversations", label: "Conversations" },
  { id: "search", label: "Search" },
  { id: "knowledge", label: "Knowledge base" },
  { id: "pricing", label: "Model pricing" },
  { id: "operations", label: "Operations" },
]

// How often the lead counter refreshes when the event stream is down. The
// counter also reloads on every handoff event, so this is the fallback.
const LEAD_POLL_MS = 60_000

// The backend publishes these as free text on the handoff event. Showing the
// raw token to an operator would be unhelpful; dropping it, which is what the
// dashboard used to do, loses the only explanation of why the lead exists.
function describeReason(reason: string | null | undefined): string {
  if (reason === "customer_started_negotiating") {
    return "The customer started negotiating a price."
  }
  if (reason === "customer_asked_for_a_human") {
    return "The customer asked to speak to a person."
  }
  return reason ? reason.replace(/_/g, " ") : "Escalated to a human operator."
}

function Login({ onSubmit }: { onSubmit: () => void }) {
  const [key, setKey] = useState("")

  return (
    <div className="login panel">
      <h2>Admin sign in</h2>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        Enter the ADMIN_API_KEY configured for this deployment.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          if (!key.trim()) return
          setApiKey(key.trim())
          onSubmit()
        }}
      >
        <input
          type="password"
          value={key}
          autoFocus
          placeholder="Admin API key"
          onChange={(event) => setKey(event.target.value)}
        />
        <button className="primary" style={{ marginTop: 12, width: "100%" }}>
          Sign in
        </button>
      </form>
    </div>
  )
}

function Shell({ onSignOut }: { onSignOut: () => void }) {
  const [view, setView] = useState<View>("overview")
  // Which conversation the transcript pane is showing. Lifted out of the
  // Conversations view so an incoming message can open one from anywhere.
  const [openConversation, setOpenConversation] = useState<number | null>(null)
  const [follow, setFollow] = useState(true)
  const toast = useToast()

  // Drives the counter on the Conversations nav item. Cheap enough to hold at
  // the shell level, and it has to live here because the badge must be
  // visible from screens where the conversation list is not mounted.
  const leads = useAsync(() => api.conversations(50, 0), [], LEAD_POLL_MS)
  const leadCount = (leads.data ?? []).filter(
    (conversation) =>
      conversation.tag === TAG_SALES_LEAD &&
      conversation.mode === MODE_HUMAN &&
      !conversation.assigned_operator,
  ).length

  function open(id: number) {
    setOpenConversation(id)
    setView("conversations")
    resetTitle()
  }

  useEvents((event) => {
    if (event.type === EVENT_HANDOFF) {
      // Ownership changed, so the queue count is stale either way.
      leads.reload()
      if (event.tag === TAG_SALES_LEAD) {
        playChime()
        flashTitle("New sales lead")
        toast.push({
          tone: "lead",
          title: "New sales lead",
          body: describeReason(event.reason),
          actionLabel: "Open conversation",
          onAction: () => {
            if (event.conversation_id !== undefined) open(event.conversation_id)
          },
        })
      }
      return
    }
    // Only a customer-initiated turn pulls the operator's attention. The bot
    // answering, or an operator's own reply, must not steal focus.
    if (event.type !== EVENT_ACTIVITY || !event.inbound) return
    if (!follow || event.conversation_id === undefined) return
    open(event.conversation_id)
  })

  return (
    <div className="layout">
      <nav className="sidebar">
        <div className="brand">
          WhatsApp <span>AI</span> Bot
        </div>
        {NAV.map((item) => (
          <button
            key={item.id}
            className={"nav-item" + (view === item.id ? " active" : "")}
            aria-current={view === item.id ? "page" : undefined}
            onClick={() => {
              setView(item.id)
              if (item.id === "conversations") resetTitle()
            }}
          >
            {item.label}
            {item.id === "conversations" && leadCount > 0 && (
              <span className="nav-count" title="Unclaimed sales leads">
                {leadCount}
              </span>
            )}
          </button>
        ))}
        <div className="sidebar-footer">
          <LiveIndicator />
          <button className="nav-item" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </nav>
      <main className="content">
        <Suspense fallback={<p className="muted">Loading...</p>}>
          {view === "overview" && <Overview />}
          {view === "customers" && <Customers />}
          {view === "conversations" && (
            <Conversations
              openId={openConversation}
              onOpen={setOpenConversation}
              follow={follow}
              onFollowChange={setFollow}
              onChanged={leads.reload}
            />
          )}
          {view === "search" && <Search onOpenConversation={open} />}
          {view === "knowledge" && <Knowledge />}
          {view === "pricing" && <Pricing />}
          {view === "operations" && <Operations />}
        </Suspense>
      </main>
    </div>
  )
}

export default function App() {
  const [authed, setAuthed] = useState(Boolean(getApiKey()))

  // api.ts clears the dead key and fires this. Without it the dashboard sat
  // showing "401: Invalid API key" forever with no route back to sign-in.
  useEffect(() => {
    function onUnauthorized() {
      resetTitle()
      setAuthed(false)
    }
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
  }, [])

  if (!authed) return <Login onSubmit={() => setAuthed(true)} />

  // The provider owns the socket, so it must sit above anything that listens.
  return (
    <EventsProvider>
      <ToastProvider>
        <Shell
          onSignOut={() => {
            clearApiKey()
            resetTitle()
            setAuthed(false)
          }}
        />
      </ToastProvider>
    </EventsProvider>
  )
}
