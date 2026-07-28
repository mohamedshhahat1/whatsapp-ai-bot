import { useState } from "react"

import { clearApiKey, getApiKey, setApiKey } from "./api"
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

export default function App() {
  const [authed, setAuthed] = useState(Boolean(getApiKey()))
  const [view, setView] = useState<View>("overview")

  if (!authed) return <Login onSubmit={() => setAuthed(true)} />

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
          <button
            className="nav-item"
            onClick={() => {
              clearApiKey()
              setAuthed(false)
            }}
          >
            Sign out
          </button>
        </div>
      </nav>
      <main className="content">
        {view === "overview" && <Overview />}
        {view === "customers" && <Customers />}
        {view === "conversations" && <Conversations />}
        {view === "search" && <Search />}
        {view === "knowledge" && <Knowledge />}
        {view === "pricing" && <Pricing />}
      </main>
    </div>
  )
}
