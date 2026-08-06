"""Interactive WhatsApp buttons: the selection ids and their labels.

Why the id and the label are separate
-------------------------------------
WhatsApp echoes the id of whatever the customer tapped back to us in the
webhook, alongside the title that was displayed. Routing on the title is the
obvious shortcut and it is a trap: the title is marketing copy. Somebody will
eventually shorten \u0637\u0644\u0628 \u0639\u0631\u0636 \u0633\u0639\u0631 to fit a button, or translate a label, or test two
wordings against each other -- and every one of those edits would silently
break lead routing, with no failing test and no error in the logs, because a
string comparison against copy that no longer exists simply returns False.

So the ids below are a contract, not a convenience. They are never translated
and never reworded. The labels beside them can change freely.

The ids also outlive the message that carried them. List menus have been
removed, but the ones already delivered are still sitting in people's chat
histories, and a customer can tap a row on one of them days later, in a
session that has since been closed and reopened. That tap arrives carrying
the id it was created with. This is the concrete reason these constants are
append-only, and why every id is still routed even though nothing sends the
service rows any more.

Why this is worth doing at all
------------------------------
A tapped button is an unambiguous statement of intent that costs no OpenAI
call to interpret. The numbered text menu it replaces asked the customer to
type, which produced \"2\", \"\u0627\u0644\u062a\u0627\u0646\u064a\", \"\u0639\u0627\u064a\u0632 \u0627\u0644\u062a\u0634\u0637\u064a\u0628\" and typos of all three, each of
which had to be sent to the model to be understood. Here the intent arrives
already classified, so the sales-lead tag is set from a fact rather than an
inference.

Why the labels live here and not in persona.py
----------------------------------------------
persona.py owns prose the customer reads as a message. These are interface
labels with a hard length limit imposed by WhatsApp (20 characters on a
button) and an id attached to each. Keeping them beside their ids is what
makes the pairing reviewable.
"""

# Selection ids. APPEND-ONLY: see the module docstring. Never reword, never
# translate, never reuse a retired id for a different meaning.
SERVICE_FINISHING = "service_finishing"
SERVICE_COMMERCIAL = "service_commercial"
SERVICE_CONTRACTING = "service_contracting"
VIEW_PORTFOLIO = "view_portfolio"
REQUEST_QUOTE = "request_quote"
REQUEST_VISIT = "request_visit"
REQUEST_CALLBACK = "request_callback"
TALK_TO_EMPLOYEE = "talk_to_employee"

#: ``(selection id, title)``.
Labelled = tuple[str, str]

_SERVICES: list[Labelled] = [
    (
        SERVICE_FINISHING,
        "\u062a\u0634\u0637\u064a\u0628 \u0634\u0642\u0629 \u0623\u0648 \u0641\u064a\u0644\u0627",
    ),
    (
        SERVICE_COMMERCIAL,
        "\u062a\u0634\u0637\u064a\u0628 \u0645\u062d\u0644 \u0623\u0648 \u0645\u0643\u062a\u0628",
    ),
    (
        SERVICE_CONTRACTING,
        "\u0623\u0639\u0645\u0627\u0644 \u0645\u0642\u0627\u0648\u0644\u0627\u062a \u0639\u0627\u0645\u0629",
    ),
    (
        VIEW_PORTFOLIO,
        "\u0623\u0639\u0645\u0627\u0644\u0646\u0627 \u0627\u0644\u0633\u0627\u0628\u0642\u0629",
    ),
]

_REQUESTS: list[Labelled] = [
    (REQUEST_QUOTE, "\u0637\u0644\u0628 \u0639\u0631\u0636 \u0633\u0639\u0631"),
    (REQUEST_VISIT, "\u0637\u0644\u0628 \u0645\u0639\u0627\u064a\u0646\u0629"),
    (REQUEST_CALLBACK, "\u0637\u0644\u0628 \u0627\u062a\u0635\u0627\u0644"),
    (
        TALK_TO_EMPLOYEE,
        "\u0627\u0644\u062a\u062d\u062f\u062b \u0645\u0639 \u0645\u0648\u0638\u0641",
    ),
]

#: Our own label for each id, used when we need to name a selection back to
#: the customer. Deliberately not the title WhatsApp sent us: echoing that
#: back would make the reply depend on client-supplied text.
LABELS: dict[str, str] = dict([*_SERVICES, *_REQUESTS])

KNOWN_SELECTIONS = frozenset(LABELS)

#: Picking a service says what the customer wants done, not what they want
#: from us next, so these are answered with the follow-up buttons below.
SERVICE_SELECTIONS = frozenset(
    {SERVICE_FINISHING, SERVICE_COMMERCIAL, SERVICE_CONTRACTING}
)

#: These three are a person's job and are handed straight over, tagged as a
#: sales lead. This is the accuracy gain: the tag comes from a tap, not from
#: a regex guessing at intent.
SALES_SELECTIONS = frozenset({REQUEST_QUOTE, REQUEST_VISIT, REQUEST_CALLBACK})

#: Max three, per WhatsApp. Offered after a service selection.
NEXT_STEP_BUTTONS: list[tuple[str, str]] = [
    (REQUEST_QUOTE, LABELS[REQUEST_QUOTE]),
    (REQUEST_VISIT, LABELS[REQUEST_VISIT]),
    (TALK_TO_EMPLOYEE, LABELS[TALK_TO_EMPLOYEE]),
]


def service_ack(selection_id: str) -> str:
    """Confirm a service selection and ask for the next step.

    Sent with NEXT_STEP_BUTTONS attached, and with no model call: the customer
    has told us exactly what they want, so there is nothing to interpret.
    """
    return (
        "\u062a\u0645\u0627\u0645\u060c \u0627\u062e\u062a\u064a\u0627\u0631 \u062d\u0636\u0631\u062a\u0643: "
        + LABELS[selection_id]
        + ".\n\n\u0645\u0646 \u0641\u0636\u0644\u0643 \u0627\u062e\u062a\u0631 \u0627\u0644\u062e\u0637\u0648\u0629 \u0627\u0644\u062a\u0627\u0644\u064a\u0629:"
    )
