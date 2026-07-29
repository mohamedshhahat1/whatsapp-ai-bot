import { useState } from "react"

import { clearApiKey, getApiKey, setApiKey } from "./api"
import { EventsProvider, LiveIndicator, useEvents } from "./events"
import Conversations from "./views/Conversations"
import Customers from "./views/Customers"
import Knowledge from "./views/Knowledge"
import Overview from "./views/Overview"
import Pricing from "./views/Pricing"
import Search from "./views/Search"

type View =
  | "overview"
  | "customers"
  | "conversations"
  | "search"
  | "knowledge"
  | "pricing"

const NAV: { id: View; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "customers", label: "Customers" },
  { id: "conversations", label: "Conversations" },
  { id: "search", label: "Search" },
  { id: "knowledge", label: "Knowledge base" },
  { id: "pricing", label: "Model pricing" },
]

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

  useEvents((event) => {
    // Only a customer-initiated turn pulls the operator's attention. The bot
    // answering, or an operator's own reply, must not steal focus.
    if (event.type !== "conversation.activity" || !event.inbound) return
    if (!follow || event.conversation_id === undefined) return
    setOpenConversation(event.conversation_id)
    setView("conversations")
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
            onClick={() => setView(item.id)}
          >
            {item.label}
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
        {view === "overview" && <Overview />}
        {view === "customers" && <Customers />}
        {view === "conversations" && (
          <Conversations
            openId={openConversation}
            onOpen={setOpenConversation}
            follow={follow}
            onFollowChange={setFollow}
          />
        )}
        {view === "search" && <Search />}
        {view === "knowledge" && <Knowledge />}
        {view === "pricing" && <Pricing />}
      </main>
    </div>
  )
}

export default function App() {
  const [authed, setAuthed] = useState(Boolean(getApiKey()))

  if (!authed) return <Login onSubmit={() => setAuthed(true)} />

  // The provider owns the socket, so it must sit above anything that listens.
  return (
    <EventsProvider>
      <Shell
        onSignOut={() => {
          clearApiKey()
          setAuthed(false)
        }}
      />
    </EventsProvider>
  )
}
