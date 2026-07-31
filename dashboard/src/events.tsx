// One WebSocket for the whole dashboard, carrying activity events.
//
// The server publishes only a pointer -- which conversation changed, and
// whether the customer started the turn. Views react by refetching through the
// authenticated /admin API, so the socket never becomes a second, weaker copy
// of the API's data or its access control.
//
// The handoff event is the exception: it carries mode, operator, reason and
// tag so that a dashboard can raise a sales lead without a round trip. Those
// four fields were on the wire from the first release and were silently
// dropped here, which is why nothing could react to a lead in real time.

import type { ReactNode } from "react"
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react"

import type { ConversationMode, ConversationTag } from "./api"
import { getApiKey } from "./api"

export const EVENT_ACTIVITY = "conversation.activity"
export const EVENT_HANDOFF = "conversation.handoff"

export interface ActivityEvent {
  type: string
  conversation_id?: number
  inbound?: boolean
  at?: string
  // Present on conversation.handoff only.
  mode?: ConversationMode
  assigned_operator?: string | null
  reason?: string | null
  tag?: ConversationTag | null
}

type Handler = (event: ActivityEvent) => void

interface EventsState {
  // True only after the server confirms the subscription, so a view can trust
  // that it will hear about changes and stop polling.
  connected: boolean
  // Set when the key was rejected: reconnecting cannot fix that.
  authFailed: boolean
  subscribe: (handler: Handler) => () => void
}

const EventsContext = createContext<EventsState>({
  connected: false,
  authFailed: false,
  subscribe: () => () => {},
})

const RECONNECT_MIN_MS = 1_000
const RECONNECT_MAX_MS = 30_000

// 1008 policy violation: the server rejected the admin key.
const POLICY_VIOLATION = 1008

function socketUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:"
  return `${protocol}//${window.location.host}/ws/events`
}

export function EventsProvider({ children }: { children: ReactNode }) {
  const [connected, setConnected] = useState(false)
  const [authFailed, setAuthFailed] = useState(false)

  // Handlers live in a ref so that a view subscribing or unsubscribing never
  // tears down and rebuilds the socket.
  const handlers = useRef(new Set<Handler>())

  const subscribe = useCallback((handler: Handler) => {
    handlers.current.add(handler)
    return () => {
      handlers.current.delete(handler)
    }
  }, [])

  useEffect(() => {
    let socket: WebSocket | null = null
    let retryMs = RECONNECT_MIN_MS
    let timer = 0
    let disposed = false

    function connect() {
      const ws = new WebSocket(socketUrl())
      socket = ws

      ws.onopen = () => {
        // A browser cannot set headers on a WebSocket, so the key goes in the
        // first frame rather than the URL, where it would land in access logs.
        ws.send(JSON.stringify({ api_key: getApiKey() }))
      }

      ws.onmessage = (raw) => {
        let event: ActivityEvent
        try {
          event = JSON.parse(String(raw.data)) as ActivityEvent
        } catch {
          return
        }
        if (event.type === "ready") {
          setConnected(true)
          retryMs = RECONNECT_MIN_MS
          return
        }
        if (event.type === "heartbeat") return
        handlers.current.forEach((handler) => handler(event))
      }

      ws.onerror = () => ws.close()

      ws.onclose = (closeEvent) => {
        setConnected(false)
        if (disposed) return
        if (closeEvent.code === POLICY_VIOLATION) {
          // Retrying with the same rejected key would just hammer the server.
          setAuthFailed(true)
          return
        }
        timer = window.setTimeout(connect, retryMs)
        retryMs = Math.min(retryMs * 2, RECONNECT_MAX_MS)
      }
    }

    connect()

    return () => {
      disposed = true
      window.clearTimeout(timer)
      socket?.close()
    }
  }, [])

  return (
    <EventsContext.Provider value={{ connected, authFailed, subscribe }}>
      {children}
    </EventsContext.Provider>
  )
}

// Run a handler on every activity event. The handler is kept in a ref, so it
// always sees current props and state without resubscribing on each render.
export function useEvents(handler: Handler): void {
  const { subscribe } = useContext(EventsContext)
  const latest = useRef(handler)

  useEffect(() => {
    latest.current = handler
  }, [handler])

  useEffect(() => subscribe((event) => latest.current(event)), [subscribe])
}

export function useEventsStatus(): EventsState {
  return useContext(EventsContext)
}

// Shown in the sidebar: an operator relying on the screen updating itself
// needs to know when it has stopped doing that.
export function LiveIndicator() {
  const { connected, authFailed } = useEventsStatus()
  const label = authFailed
    ? "Live updates unauthorized"
    : connected
      ? "Live"
      : "Reconnecting - polling"
  return (
    <div
      className={authFailed ? "error" : "muted"}
      style={{ fontSize: 12, padding: "8px 4px" }}
    >
      {connected ? "\u25cf " : "\u25cb "}
      {label}
    </div>
  )
}
