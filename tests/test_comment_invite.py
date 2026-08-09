"""The comment-to-DM invitation, against the real schema.

What stops a customer being invited twice is a UNIQUE index resolving an
``ON CONFLICT DO NOTHING`` -- not a branch anybody wrote. A stubbed repository
agreeing with the code would pass whether or not that index exists, so the
duplicate-protection tests here run against real Postgres and let it be the
thing that refuses.

Nothing mocks the adapter or the flow under test. Both adapters are the real
ones with their transport swapped for a recording ``MockTransport``, so the
URL, the JSON body and the error translation are exercised as written.

The two router tests stand in for ``ChatService`` and nothing else. It has its
own tests, and constructing it for real would pull OpenAI and Redis into a
test whose whole subject is which branch ``process_meta_payload`` takes.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import BaseChannelAdapter
from app.channels.config import (
    DEFAULT_FACEBOOK_COMMENT_DM_INVITE,
    DEFAULT_INSTAGRAM_COMMENT_DM_INVITE,
    ChannelSettings,
)
from app.channels.constants import (
    FACEBOOK_COMMENT,
    INSTAGRAM_COMMENT,
    INSTAGRAM_DM,
)
from app.channels.events import EVENT_TEXT, InboundEvent
from app.channels.facebook_comments import FacebookCommentAdapter
from app.channels.instagram_comments import InstagramCommentAdapter
from app.config import Settings
from app.models.message import STATUS_SENT, STATUS_UNCONFIRMED, Message
from app.services import webhook_processor
from app.services.comment_invite import (
    INVITE_TYPE,
    invite_after_comment,
    reservation_key,
)
from tests.conftest import new_external_id, purge_channel

PAGE_ID = "100000000000001"
IG_ACCOUNT = "17841400000000001"
FB_COMMENT_ID = "100000000000001_200000000000002"
IG_COMMENT_ID = "17900000000000001"
POST_ID = "100000000000001_300000000000003"

#: Two bytes a letter in UTF-8, which is what the byte-measured clamps care
#: about. Pinned so a future edit cannot quietly make it single byte.
ARABIC_LETTER = "ا"


def _app_settings() -> Settings:
    """Application settings. ``_env_file=None`` keeps a local .env out."""
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _facebook_settings(**overrides: Any) -> ChannelSettings:
    """Facebook comment settings with the invitation switched ON.

    Defaults are merged rather than splatted alongside ``**overrides``: a test
    that overrides one of the pinned keys would otherwise raise TypeError for
    a duplicate keyword argument, which mypy does not catch because tests are
    excluded from it.
    """
    values: dict[str, Any] = {
        "enable_facebook_comments": True,
        "facebook_page_id": PAGE_ID,
        "facebook_page_access_token": "unit-test-placeholder",
        "facebook_comment_dm_invite": True,
    }
    values.update(overrides)
    return ChannelSettings(_env_file=None, **values)


def _instagram_settings(**overrides: Any) -> ChannelSettings:
    """Instagram comment settings with the invitation switched ON.

    ``facebook_page_id`` is present because Meta addresses an Instagram
    private reply to the LINKED PAGE, even though this channel's own
    credentials are the Instagram pair.
    """
    values: dict[str, Any] = {
        "enable_instagram_comments": True,
        "facebook_page_id": PAGE_ID,
        "instagram_account_id": IG_ACCOUNT,
        "instagram_access_token": "unit-test-placeholder",
        "instagram_comment_dm_invite": True,
    }
    values.update(overrides)
    return ChannelSettings(_env_file=None, **values)


def _recording(
    adapter: FacebookCommentAdapter | InstagramCommentAdapter,
    status: int = 200,
    body: dict[str, Any] | None = None,
) -> list[httpx.Request]:
    """Swap a real adapter's transport for one that records instead of sends."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        default = {"recipient_id": "psid-1", "message_id": "mid.private.1"}
        return httpx.Response(status, json=body if body is not None else default)

    adapter._client = httpx.AsyncClient(
        base_url=str(adapter._client.base_url),
        transport=httpx.MockTransport(handler),
    )
    return seen


def _comment_event(
    channel: str,
    sender_id: str,
    comment_id: str,
    **overrides: Any,
) -> InboundEvent:
    """A normalised comment, as an adapter would have produced it."""
    values: dict[str, Any] = {
        "channel": channel,
        "sender_id": sender_id,
        "sender_name": "Customer",
        "provider_message_id": comment_id,
        "kind": EVENT_TEXT,
        "text": "كم سعر التشطيب؟",
        "context": {"comment_id": comment_id},
    }
    values.update(overrides)
    return InboundEvent(**values)


async def _invitations(session: AsyncSession, comment_id: str) -> list[Message]:
    """Every stored invitation for one comment. Should never exceed one."""
    rows = await session.scalars(
        select(Message).where(
            Message.reply_to_wa_message_id == reservation_key(comment_id)
        )
    )
    return list(rows)


@pytest.fixture
async def commenter(db: AsyncSession) -> AsyncIterator[str]:
    """A commenter with no rows yet, cleaned up on both comment surfaces.

    The invitation resolves its own customer through ``get_channel_context``,
    so nothing is created up front -- that resolution is part of what is being
    tested.
    """
    external_id = new_external_id("commenter")
    try:
        yield external_id
    finally:
        await purge_channel(db, FACEBOOK_COMMENT, external_id)
        await purge_channel(db, INSTAGRAM_COMMENT, external_id)


# --- Facebook ---------------------------------------------------------------


async def test_an_invitation_is_sent_and_recorded(
    db: AsyncSession, commenter: str
) -> None:
    """The private reply goes out and leaves a confirmed row behind."""
    adapter = FacebookCommentAdapter(_facebook_settings())
    seen = _recording(adapter)
    event = _comment_event(FACEBOOK_COMMENT, commenter, FB_COMMENT_ID)

    sent = await invite_after_comment(db, adapter, _app_settings(), event)

    assert sent is True
    assert len(seen) == 1
    assert seen[0].url.path.endswith("/" + PAGE_ID + "/messages")
    body = json.loads(seen[0].content)
    assert body["recipient"] == {"comment_id": FB_COMMENT_ID}
    assert body["message"]["text"] == DEFAULT_FACEBOOK_COMMENT_DM_INVITE

    (row,) = await _invitations(db, FB_COMMENT_ID)
    assert row.direction == "outbound"
    assert row.type == INVITE_TYPE
    assert row.status == STATUS_SENT
    assert row.content == DEFAULT_FACEBOOK_COMMENT_DM_INVITE
    assert row.wa_message_id == "mid.private.1"


async def test_a_switched_off_surface_sends_nothing(
    db: AsyncSession, commenter: str
) -> None:
    """Off is the default, and off means no request and no row."""
    adapter = FacebookCommentAdapter(
        _facebook_settings(facebook_comment_dm_invite=False)
    )
    seen = _recording(adapter)
    event = _comment_event(FACEBOOK_COMMENT, commenter, FB_COMMENT_ID)

    sent = await invite_after_comment(db, adapter, _app_settings(), event)

    assert sent is False
    assert seen == []
    assert await _invitations(db, FB_COMMENT_ID) == []


async def test_the_same_comment_is_only_ever_invited_once(
    db: AsyncSession, commenter: str
) -> None:
    """Meta permits exactly one private reply per commenter.

    The second call is what a redelivery looks like. What refuses it is the
    unique index on reply_to_wa_message_id, which is why this test needs a
    real database to mean anything.
    """
    adapter = FacebookCommentAdapter(_facebook_settings())
    seen = _recording(adapter)
    event = _comment_event(FACEBOOK_COMMENT, commenter, FB_COMMENT_ID)
    settings = _app_settings()

    first = await invite_after_comment(db, adapter, settings, event)
    second = await invite_after_comment(db, adapter, settings, event)

    assert first is True
    assert second is False
    assert len(seen) == 1
    assert len(await _invitations(db, FB_COMMENT_ID)) == 1


async def test_the_copy_comes_from_settings_not_from_the_code(
    db: AsyncSession, commenter: str
) -> None:
    """Replacing the temporary wording must stay a configuration change."""
    wording = "مرحباً، " + ARABIC_LETTER * 3
    adapter = FacebookCommentAdapter(
        _facebook_settings(facebook_comment_dm_invite_message=wording)
    )
    seen = _recording(adapter)
    event = _comment_event(FACEBOOK_COMMENT, commenter, FB_COMMENT_ID)

    await invite_after_comment(db, adapter, _app_settings(), event)

    assert json.loads(seen[0].content)["message"]["text"] == wording
    (row,) = await _invitations(db, FB_COMMENT_ID)
    assert row.content == wording


async def test_a_refused_invitation_is_kept_unconfirmed_and_never_retried(
    db: AsyncSession, commenter: str
) -> None:
    """A refusal is ordinary traffic: recorded, not raised, not retried.

    The reservation is deliberately kept rather than released. The failures
    that reach here are largely ambiguous -- a timeout may well have delivered
    the invitation -- and a second unsolicited DM is worse than none.
    """
    adapter = FacebookCommentAdapter(_facebook_settings())
    seen = _recording(adapter, status=400)
    event = _comment_event(FACEBOOK_COMMENT, commenter, FB_COMMENT_ID)
    settings = _app_settings()

    sent = await invite_after_comment(db, adapter, settings, event)

    assert sent is False
    (row,) = await _invitations(db, FB_COMMENT_ID)
    assert row.status == STATUS_UNCONFIRMED
    assert row.wa_message_id is None

    # The redelivery finds the reservation and declines to send again.
    assert await invite_after_comment(db, adapter, settings, event) is False
    assert len(seen) == 1


async def test_an_unroutable_comment_is_not_invited(
    db: AsyncSession, commenter: str
) -> None:
    """No comment id means nothing to address and nothing to dedupe on."""
    adapter = FacebookCommentAdapter(_facebook_settings())
    seen = _recording(adapter)
    event = _comment_event(FACEBOOK_COMMENT, commenter, "", provider_message_id="")

    assert await invite_after_comment(db, adapter, _app_settings(), event) is False
    assert seen == []


# --- Instagram --------------------------------------------------------------


async def test_an_instagram_invitation_is_addressed_to_the_linked_page(
    db: AsyncSession, commenter: str
) -> None:
    """Not the Instagram account, and not /me -- Meta documents the Page."""
    adapter = InstagramCommentAdapter(_instagram_settings())
    seen = _recording(adapter)
    event = _comment_event(INSTAGRAM_COMMENT, commenter, IG_COMMENT_ID)

    sent = await invite_after_comment(db, adapter, _app_settings(), event)

    assert sent is True
    assert len(seen) == 1
    assert seen[0].url.path.endswith("/" + PAGE_ID + "/messages")
    body = json.loads(seen[0].content)
    assert body["recipient"] == {"comment_id": IG_COMMENT_ID}
    assert body["message"]["text"] == DEFAULT_INSTAGRAM_COMMENT_DM_INVITE
    # Belongs to the ordinary Send API, not the private reply contract.
    assert "messaging_type" not in body


async def test_an_instagram_invitation_without_a_page_id_fails_safely(
    db: AsyncSession, commenter: str
) -> None:
    """A deployment that only answers publicly never sets the page id.

    The adapter refuses before reaching the network, and the caller records
    that refusal instead of letting it fail the whole delivery.
    """
    adapter = InstagramCommentAdapter(_instagram_settings(facebook_page_id=""))
    seen = _recording(adapter)
    event = _comment_event(INSTAGRAM_COMMENT, commenter, IG_COMMENT_ID)

    sent = await invite_after_comment(db, adapter, _app_settings(), event)

    assert sent is False
    assert seen == []
    (row,) = await _invitations(db, IG_COMMENT_ID)
    assert row.status == STATUS_UNCONFIRMED


async def test_the_two_surfaces_are_switched_independently(
    db: AsyncSession, commenter: str
) -> None:
    """Facebook on does not turn Instagram on."""
    adapter = InstagramCommentAdapter(
        _instagram_settings(instagram_comment_dm_invite=False)
    )
    seen = _recording(adapter)
    event = _comment_event(INSTAGRAM_COMMENT, commenter, IG_COMMENT_ID)

    assert await invite_after_comment(db, adapter, _app_settings(), event) is False
    assert seen == []


async def test_the_two_surfaces_carry_their_own_wording(
    db: AsyncSession, commenter: str
) -> None:
    """Kept separate so a Reel audience can be addressed differently later."""
    facebook = _facebook_settings(facebook_comment_dm_invite_message="نص فيسبوك")
    instagram = _instagram_settings(instagram_comment_dm_invite_message="نص انستجرام")

    assert facebook.dm_invite_message(FACEBOOK_COMMENT) == "نص فيسبوك"
    assert instagram.dm_invite_message(INSTAGRAM_COMMENT) == "نص انستجرام"
    # Neither surface can read the other's copy.
    assert facebook.dm_invite_message(INSTAGRAM_COMMENT) == (
        DEFAULT_INSTAGRAM_COMMENT_DM_INVITE
    )


async def test_a_blank_override_falls_back_rather_than_sending_nothing(
    db: AsyncSession, commenter: str
) -> None:
    """An empty key in .env must not become an empty direct message."""
    adapter = InstagramCommentAdapter(
        _instagram_settings(instagram_comment_dm_invite_message="   ")
    )
    seen = _recording(adapter)
    event = _comment_event(INSTAGRAM_COMMENT, commenter, IG_COMMENT_ID)

    await invite_after_comment(db, adapter, _app_settings(), event)

    body = json.loads(seen[0].content)
    assert body["message"]["text"] == DEFAULT_INSTAGRAM_COMMENT_DM_INVITE


# --- The router branch ------------------------------------------------------


class _SilentChatService:
    """Stands in for the orchestration, which has its own tests.

    Constructing the real one would pull OpenAI and Redis into a test whose
    subject is which branch process_meta_payload takes.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.channel = kwargs.get("channel")

    async def handle_text_message(self, *args: Any) -> None:
        return None

    async def handle_unsupported_message(self, *args: Any) -> None:
        return None

    async def handle_media_message(self, *args: Any) -> None:
        return None

    async def handle_interactive_message(self, *args: Any) -> None:
        return None


class _PrivateAdapter(BaseChannelAdapter):
    """A DM adapter: not a CommentChannelAdapter, so it has no invitation."""

    channel = INSTAGRAM_DM

    def __init__(self, event: InboundEvent) -> None:
        self._event = event

    def parse(self, payload: dict[str, Any]) -> list[InboundEvent]:
        return [self._event]

    async def send_text(self, recipient: str, text: str) -> dict[str, Any]:
        return {"message_id": "mid.dm.1"}


def _facebook_delivery(commenter: str) -> dict[str, Any]:
    """One Page webhook delivery carrying a single fresh comment.

    created_time is now, in epoch SECONDS as this field is documented, so the
    inbound freshness gate lets it through.
    """
    return {
        "object": "page",
        "entry": [
            {
                "id": PAGE_ID,
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "from": {"id": commenter, "name": "Customer"},
                            "item": "comment",
                            "verb": "add",
                            "comment_id": FB_COMMENT_ID,
                            "post_id": POST_ID,
                            "created_time": int(datetime.now(UTC).timestamp()),
                            "message": "عايز عرض سعر",
                        },
                    }
                ],
            }
        ],
    }


async def test_a_comment_delivery_invites_through_the_real_router(
    db: AsyncSession,
    commenter: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole path: webhook payload in, private reply out, row written.

    The adapter parses for real, the router takes its comment branch for real,
    and the invitation reserves against the real unique index.
    """
    monkeypatch.setattr(webhook_processor, "ChatService", _SilentChatService)
    adapter = FacebookCommentAdapter(_facebook_settings())
    seen = _recording(adapter)

    await webhook_processor.process_meta_payload(
        db,
        adapter,
        object(),
        _app_settings(),
        _facebook_delivery(commenter),
    )

    assert len(seen) == 1
    assert seen[0].url.path.endswith("/" + PAGE_ID + "/messages")
    (row,) = await _invitations(db, FB_COMMENT_ID)
    assert row.status == STATUS_SENT


async def test_a_private_channel_delivery_never_reaches_the_invitation(
    db: AsyncSession,
    commenter: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WhatsApp, Messenger and Instagram DM behaviour is unchanged.

    The branch is not merely false for a DM adapter -- it is unreachable,
    because a DM adapter is not a CommentChannelAdapter. This asserts the
    invitation is not even consulted.
    """
    monkeypatch.setattr(webhook_processor, "ChatService", _SilentChatService)
    called: list[str] = []

    async def _record(*args: Any, **kwargs: Any) -> bool:
        called.append("invited")
        return False

    monkeypatch.setattr(webhook_processor, "invite_after_comment", _record)
    event = _comment_event(INSTAGRAM_DM, commenter, "mid.in.1")

    await webhook_processor.process_meta_payload(
        db,
        _PrivateAdapter(event),
        object(),
        _app_settings(),
        {"object": "instagram"},
    )

    assert called == []
