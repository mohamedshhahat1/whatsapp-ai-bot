"""Conversation response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.channels.constants import WHATSAPP
from app.config import get_settings
from app.models.conversation import derive_session_state
from app.schemas.message import MessageRead


class ConversationRead(BaseModel):
    """One conversation -- that is, one session -- as clients see it.

    Note what the id means now that sessions end: this identifies a VISIT, not
    a customer. One customer produces many of these over time, they are not
    merged, and ``user_id`` is the only stable per-customer key. A client that
    assumes one conversation per customer will show the same person several
    times over, each with a different transcript.

    Every lifecycle field below is nullable or defaulted, so this stayed
    backward compatible when they were added: an older client that reads only
    the original fields is unaffected.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    # Lifecycle and ownership are separate axes: a conversation stays "active"
    # for the whole time a human operator owns it.
    #
    # "active" or "closed". Note for client authors: "archived" has never been
    # emitted by this API despite appearing in some older client type
    # declarations.
    status: str
    mode: str
    # Why a person is needed -- currently only "sales_lead" or nothing. The
    # operator list sorts unclaimed leads to the top, so the dashboard needs
    # this to render the badge that explains the ordering. A list that
    # reorders itself with no visible reason looks like a bug.
    tag: str | None = None
    assigned_operator: str | None = None
    handoff_at: datetime | None = None
    # --- Session lifecycle --------------------------------------------------
    # When the idle countdown last restarted. Both directions of traffic reset
    # it, so this is not "last customer message"; a client showing time
    # remaining should count from here.
    last_activity_at: datetime | None = None
    # Non-null once this session has greeted its customer. A reopened session
    # keeps its original value, which is exactly why nobody is greeted twice.
    welcome_sent_at: datetime | None = None
    # Non-null once a worker has CLAIMED the goodbye -- not necessarily once
    # one has been delivered. The claim is committed before the send precisely
    # so that a crash cannot produce two goodbyes, which means this can be set
    # for a message that never went out.
    closing_sent_at: datetime | None = None
    # When the session ended. Null on an active session, and null again after
    # a reopen.
    closed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def session_state(self) -> str:
        """Lifecycle state: ACTIVE_BOT, ACTIVE_HUMAN, WAITING_IDLE, CLOSING,
        CLOSED.

        Computed rather than stored, and computed by the same function the
        backend uses, so a client can never be told a state the closing logic
        disagrees with.

        WAITING_IDLE means the session is past its idle timeout and due to be
        closed by the next sweep -- it has not closed yet. CLOSING means a
        worker has already claimed it and the close is moments away; a UI
        should not offer to reply at that point without expecting a reopen.

        There is no REOPENED state, and that is a limitation worth stating
        rather than papering over. Reviving a session clears ``closed_at`` and
        ``closing_sent_at``, so a reopened conversation is indistinguishable
        from one that never closed. Reporting it would require storing a
        ``reopened_at`` column; it cannot be derived from what exists, and
        guessing would be worse than admitting the gap.
        """
        return derive_session_state(
            status=self.status,
            mode=self.mode,
            last_activity_at=self.last_activity_at,
            closing_sent_at=self.closing_sent_at,
            idle_after=get_settings().conversation_idle_timeout,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def idle_timeout_minutes(self) -> int:
        """The configured idle timeout, so a client can show a countdown.

        Served per conversation rather than from a settings endpoint because
        every client that needs it already has this payload, and a second
        round trip to learn one integer is a worse trade than a few bytes.
        """
        return int(get_settings().conversation_idle_timeout.total_seconds() // 60)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def close_after_idle(self) -> bool:
        """Whether idle sessions actually close, or merely report as idle.

        With CONVERSATION_CLOSE_AFTER_IDLE off, WAITING_IDLE is a permanent
        resting state rather than a countdown, and a UI promising "closes in
        two minutes" would be lying.
        """
        return get_settings().conversation_close_after_idle


class ConversationDetail(ConversationRead):
    messages: list[MessageRead]


class ConversationSummary(BaseModel):
    """One of a customer's other visits, for the operator history panel.

    Enough to date a session and say how big it was, without its transcript:
    the panel lists several and loading every message of each would be a large
    payload for a sidebar nobody has clicked yet.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    mode: str
    tag: str | None = None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None


class CustomerHistory(BaseModel):
    """The customer behind a conversation, and their other sessions.

    Exists because sessions are separate rows and deliberately stay that way.
    Merging them would hide the gaps between visits, which are the entire
    point of the lifecycle -- but an operator answering someone still needs to
    know this is their fifth time asking.

    None of this reaches the model. Prompt context is built from the current
    session alone; widening it would change the bot's answers in ways nobody
    asked for.

    Identity is reported three ways now that the platform is not WhatsApp-only.
    ``channel`` is the app they write from, ``external_id`` is their id on it,
    and ``wa_id`` is a phone number or nothing. New clients should read the
    first two.
    """

    user_id: int
    # WhatsApp only, and empty for a customer who arrived anywhere else.
    # Deliberately NOT widened to ``str | None``: the dashboard and the
    # Flutter app both declare this a plain string, so making it nullable
    # would break them to describe a field that is being superseded rather
    # than extended. A page-scoped Messenger id is not written here either --
    # that would satisfy the type and still be a lie, since anything
    # rendering this as a phone number would render a PSID as one.
    wa_id: str = ""
    # "whatsapp", "messenger", and so on. Defaulted rather than required so
    # that nothing constructing this by hand had to change: every customer
    # who existed before channels did is on WhatsApp.
    channel: str = WHATSAPP
    # Their id on ``channel``: the phone number for WhatsApp, a page-scoped id
    # for Messenger. The only identity field populated for every channel, so
    # this is the one a client should key off going forward.
    external_id: str | None = None
    name: str | None = None
    total_conversations: int = Field(
        description="Every session this customer has ever had, including this " "one."
    )
    previous: list[ConversationSummary] = Field(
        default_factory=list,
        description="Their other sessions, most recent first.",
    )


class HandoffRequest(BaseModel):
    """Body for taking a conversation over. The operator name is optional.

    There are no operator accounts yet, so this is a label the dashboard sends
    to stop two people answering the same customer -- not an authenticated
    identity.
    """

    operator: str | None = Field(
        default=None,
        max_length=64,
        description="Name of the operator taking the conversation over.",
    )
