"""Channel abstraction: one AI backend, many transports.

The rule this package exists to enforce is that nothing below the adapter
knows which app the customer is writing from. ChatService, PromptBuilder, the
RAG pipeline, the session lifecycle, the handoff and the sales-lead tagging
are written once and handed a normalised event; the adapter is the only thing
that knows what a Graph API envelope looks like.

Read ``constants.py`` first -- what a channel can do is data there, not a
branch at the call site -- then ``events.py`` for the normalised inbound
shape, then ``base.py`` for the outbound contract.

Deliberately no re-exports here. Every import in this package is a leaf, and
keeping __init__ empty means importing the WhatsApp adapter never drags the
Instagram HTTP client into the worker's import graph.
"""
