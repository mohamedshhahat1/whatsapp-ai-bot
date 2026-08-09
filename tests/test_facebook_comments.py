"""Facebook Page comment adapter: normalisation, guards and the two replies.

These tests drive the real adapter over a real ``httpx`` client whose
transport is swapped for a recording one, so the URL, the JSON body and the
error translation are all exercised as written. Nothing mocks the adapter
itself -- a test that replaced ``parse`` or ``_post`` would pass no matter what
the contract with Meta actually is, which is the whole thing being checked.

The payloads are the shapes recorded in docs/CHANNELS.md, taken from Meta's
Page webhook reference rather than from the Messenger adapter.
"""

from typing import Any

import httpx
import pytest

from app.channels.config import (
    DEFAULT_FACEBOOK_COMMENT_DM_INVITE,
    ChannelSettings,
)
from app.channels.constants import FACEBOOK_COMMENT
from app.channels.events import EVENT_TEXT, EVENT_UNSUPPORTED
from app.channels.facebook_comments import FacebookCommentAdapter
from app.core.exceptions import ExternalServiceError

PAGE_ID = "100000000000001"
COMMENTER = "7000000000000001"
COMMENT_ID = "100000000000001_200000000000002"
POST_ID = "100000000000001_300000000000003"

# 2025-01-01T00:00:00Z in epoch SECONDS. Read as milliseconds this becomes
# 1970-01-21, which is what the freshness gate would then discard.
CREATED_TIME = 1735689600


def _settings(**overrides: Any) -> ChannelSettings:
    """Channel settings with the Facebook credentials filled in.

    Defaults are merged rather than splatted alongside ``**overrides``: a test
    that overrides one of the pinned keys would otherwise raise TypeError for
    a duplicate keyword argument at call time, which mypy does not catch here
    because tests are excluded from it.
    """
    values: dict[str, Any] = {
        "enable_facebook_comments": True,
        "facebook_page_id": PAGE_ID,
        "facebook_page_access_token": "unit-test-placeholder",
    }
    values.update(overrides)
    return ChannelSettings(_env_file=None, **values)


def _adapter(
    status: int = 200,
    body: dict[str, Any] | None = None,
    **overrides: Any,
) -> tuple[FacebookCommentAdapter, list[httpx.Request]]:
    """A real adapter whose transport records requests instead of sending them."""
    adapter = FacebookCommentAdapter(_settings(**overrides))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {"id": "r1"})

    adapter._client = httpx.AsyncClient(
        base_url=str(adapter._client.base_url),
        transport=httpx.MockTransport(handler),
    )
    return adapter, seen


def _delivery(**value_overrides: Any) -> dict[str, Any]:
    """One Page webhook delivery carrying a single feed change."""
    value: dict[str, Any] = {
        "from": {"id": COMMENTER, "name": "Customer"},
        "item": "comment",
        "verb": "add",
        "comment_id": COMMENT_ID,
        "post_id": POST_ID,
        "created_time": CREATED_TIME,
        "message": "كم سعر التشطيب؟",
    }
    value.update(value_overrides)
    return {
        "object": "page",
        "entry": [
            {
                "id": PAGE_ID,
                "time": CREATED_TIME,
                "changes": [{"field": "feed", "value": value}],
            }
        ],
    }


# --- Inbound normalisation --------------------------------------------------


def test_a_customer_comment_becomes_one_text_event() -> None:
    adapter, _ = _adapter()
    events = list(adapter.parse(_delivery()))

    assert len(events) == 1
    event = events[0]
    assert event.channel == FACEBOOK_COMMENT
    assert event.kind == EVENT_TEXT
    assert event.sender_id == COMMENTER
    assert event.sender_name == "Customer"
    assert event.text == "كم سعر التشطيب؟"


def test_the_comment_id_is_the_provider_message_id() -> None:
    """It is the pipeline's idempotency key, so it must be the comment's id.

    claim_inbound dedupes on this value, which is what makes a boosted post's
    duplicate notifications collapse into one stored comment.
    """
    adapter, _ = _adapter()
    (event,) = list(adapter.parse(_delivery()))

    assert event.provider_message_id == COMMENT_ID


def test_created_time_is_read_as_seconds_not_milliseconds() -> None:
    """The regression this whole adapter is most likely to grow back.

    Every messaging surface here sends milliseconds. This field does not, and
    dividing by 1000 backdates the comment to 1970 -- at which point the
    inbound freshness gate drops it and comments are silently never answered.
    """
    adapter, _ = _adapter()
    (event,) = list(adapter.parse(_delivery()))

    assert event.sent_at is not None
    assert event.sent_at.year == 2025
    assert event.sent_at.month == 1
    assert event.sent_at.day == 1


def test_the_thread_coordinates_travel_with_the_event() -> None:
    adapter, _ = _adapter()
    (event,) = list(adapter.parse(_delivery(permalink_url="https://x.test/c")))

    assert event.context["comment_id"] == COMMENT_ID
    assert event.context["page_id"] == PAGE_ID
    assert event.context["post_id"] == POST_ID
    assert event.context["permalink_url"] == "https://x.test/c"


def test_a_comment_with_no_text_is_unsupported_rather_than_dropped() -> None:
    """A photo-only comment still deserves an answer, not silence."""
    adapter, _ = _adapter()
    (event,) = list(adapter.parse(_delivery(message="")))

    assert event.kind == EVENT_UNSUPPORTED
    assert event.provider_message_id == COMMENT_ID


# --- The guards -------------------------------------------------------------


def test_the_pages_own_comment_is_ignored() -> None:
    """Otherwise the bot answers itself, and every answer is another webhook."""
    adapter, _ = _adapter()
    delivery = _delivery(**{"from": {"id": PAGE_ID, "name": "The Page"}})

    assert list(adapter.parse(delivery)) == []


def test_the_pages_own_comment_is_kept_when_the_guard_is_switched_off() -> None:
    adapter, _ = _adapter(ignore_own_comments=False)
    delivery = _delivery(**{"from": {"id": PAGE_ID, "name": "The Page"}})

    assert len(list(adapter.parse(delivery))) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"item": "post"},
        {"item": "like"},
        {"verb": "edited"},
        {"verb": "remove"},
        {"verb": "hide"},
    ],
)
def test_only_a_newly_added_comment_is_routed(overrides: dict[str, Any]) -> None:
    """field == "feed" also reports posts, likes, edits, hides and removals."""
    adapter, _ = _adapter()

    assert list(adapter.parse(_delivery(**overrides))) == []


def test_a_comment_without_an_id_is_dropped() -> None:
    adapter, _ = _adapter()

    assert list(adapter.parse(_delivery(comment_id=""))) == []


def test_an_anonymous_comment_is_dropped_rather_than_half_routed() -> None:
    """No author means no user to resolve and nobody to answer privately."""
    adapter, _ = _adapter()

    assert list(adapter.parse(_delivery(**{"from": {}}))) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"object": "page"},
        {"object": "page", "entry": None},
        {"object": "page", "entry": ["not-an-entry"]},
        {"object": "page", "entry": [{"id": PAGE_ID}]},
        {"object": "page", "entry": [{"id": PAGE_ID, "changes": None}]},
        {"object": "page", "entry": [{"id": PAGE_ID, "changes": ["nope"]}]},
        {
            "object": "page",
            "entry": [{"id": PAGE_ID, "changes": [{"field": "feed"}]}],
        },
        {
            "object": "page",
            "entry": [
                {"id": PAGE_ID, "changes": [{"field": "feed", "value": "text"}]}
            ],
        },
        {
            "object": "page",
            "entry": [{"id": PAGE_ID, "changes": [{"field": "mention"}]}],
        },
    ],
)
def test_a_malformed_delivery_yields_nothing_and_does_not_raise(
    payload: dict[str, Any],
) -> None:
    """Meta adds fields to an existing subscription; junk is normal traffic."""
    adapter, _ = _adapter()

    assert list(adapter.parse(payload)) == []


def test_several_comments_in_one_delivery_all_arrive() -> None:
    adapter, _ = _adapter()
    first = _delivery()
    second = _delivery(comment_id=COMMENT_ID + "_b", message="عايز عرض سعر")
    first["entry"].append(second["entry"][0])

    events = list(adapter.parse(first))

    assert [event.provider_message_id for event in events] == [
        COMMENT_ID,
        COMMENT_ID + "_b",
    ]


# --- Outbound ---------------------------------------------------------------


async def test_a_public_reply_posts_to_the_comments_edge() -> None:
    adapter, seen = _adapter()

    await adapter.reply_to_comment(COMMENT_ID, "شكراً لتواصلك")

    assert len(seen) == 1
    assert seen[0].url.path.endswith("/" + COMMENT_ID + "/comments")
    assert httpx._content.json.loads(seen[0].content)["message"] == "شكراً لتواصلك"


async def test_send_text_is_a_public_reply_addressed_to_the_comment() -> None:
    """send_text is ChatService's sender contract; recipient is a comment id."""
    adapter, seen = _adapter()

    await adapter.send_text(COMMENT_ID, "أهلاً")

    assert len(seen) == 1
    assert seen[0].url.path.endswith("/" + COMMENT_ID + "/comments")


async def test_a_private_reply_uses_the_comment_id_as_the_recipient() -> None:
    """Meta resolves the comment to its author; no PSID is known up front."""
    import json

    adapter, seen = _adapter(body={"recipient_id": COMMENTER, "message_id": "m1"})

    result = await adapter.invite_to_private_thread(COMMENT_ID, "رسالة خاصة")

    assert len(seen) == 1
    assert seen[0].url.path.endswith("/" + PAGE_ID + "/messages")
    body = json.loads(seen[0].content)
    assert body["recipient"] == {"comment_id": COMMENT_ID}
    assert body["message"] == {"text": "رسالة خاصة"}
    # Not part of the private reply contract.
    assert "messaging_type" not in body
    # The returned PSID is the comment-to-DM link every conversion metric needs.
    assert result["recipient_id"] == COMMENTER


async def test_a_private_reply_without_a_page_id_fails_loudly() -> None:
    adapter, seen = _adapter(facebook_page_id="")

    with pytest.raises(ExternalServiceError):
        await adapter.invite_to_private_thread(COMMENT_ID, "رسالة")

    assert seen == []


async def test_a_refused_private_reply_becomes_an_external_service_error() -> None:
    """One private reply per commenter: the second is a normal refusal."""
    adapter, _ = _adapter(status=400)

    with pytest.raises(ExternalServiceError):
        await adapter.invite_to_private_thread(COMMENT_ID, "رسالة")


async def test_a_refused_public_reply_becomes_an_external_service_error() -> None:
    adapter, _ = _adapter(status=400)

    with pytest.raises(ExternalServiceError):
        await adapter.reply_to_comment(COMMENT_ID, "مرحباً")


async def test_long_text_is_clamped_rather_than_rejected_by_the_api() -> None:
    import json

    adapter, seen = _adapter()

    await adapter.reply_to_comment(COMMENT_ID, "ا" * 5000)

    assert len(json.loads(seen[0].content)["message"]) == 2000


# --- The copy is configuration, not code ------------------------------------


def test_the_invitation_copy_comes_from_settings_not_the_adapter() -> None:
    """The adapter takes the text as a parameter and holds no copy of its own.

    The default is temporary and unreviewed on purpose; what this pins is that
    replacing it is a settings change rather than a code change.
    """
    assert _settings().dm_invite_message(FACEBOOK_COMMENT) == (
        DEFAULT_FACEBOOK_COMMENT_DM_INVITE
    )

    overridden = _settings(facebook_comment_dm_invite_message="نص بديل")
    assert overridden.dm_invite_message(FACEBOOK_COMMENT) == "نص بديل"


def test_a_blank_override_falls_back_instead_of_sending_nothing() -> None:
    """A deployer who copies .env.example must not send an empty message."""
    blank = _settings(facebook_comment_dm_invite_message="   ")

    assert blank.dm_invite_message(FACEBOOK_COMMENT) == (
        DEFAULT_FACEBOOK_COMMENT_DM_INVITE
    )
