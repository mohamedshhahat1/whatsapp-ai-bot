"""Instagram comment adapter: two envelopes, two replies, no invented data.

These tests drive the real adapter over a real ``httpx`` client whose
transport is swapped for a recording one, so the URL, the query string, the
JSON body and the error translation are all exercised as written. Nothing
mocks the adapter itself -- a test that replaced ``parse`` or ``_post`` would
pass no matter what the contract with Meta actually is, which is the whole
thing being checked.

The payloads are the two shapes Meta's Instagram webhook reference documents,
not adaptations of the Page ``feed`` payload: the id key differs between them,
and neither carries a timestamp.
"""

import json
from typing import Any

import httpx
import pytest

from app.channels import registry
from app.channels.base import CommentChannelAdapter
from app.channels.config import (
    DEFAULT_FACEBOOK_COMMENT_DM_INVITE,
    DEFAULT_INSTAGRAM_COMMENT_DM_INVITE,
    ChannelSettings,
)
from app.channels.constants import FACEBOOK_COMMENT, INSTAGRAM_COMMENT
from app.channels.events import EVENT_TEXT, EVENT_UNSUPPORTED
from app.channels.instagram_comments import InstagramCommentAdapter
from app.core.exceptions import ExternalServiceError

IG_ACCOUNT = "17841400000000001"
#: The Facebook Page linked to the Instagram account. A private reply is
#: addressed to this, which is the difference most likely to be got wrong.
PAGE_ID = "100000000000001"
COMMENTER = "6789012345678901"
COMMENT_ID = "17900000000000001"
MEDIA_ID = "17800000000000002"

#: Written as literal characters rather than \\uXXXX escapes: an escape costs
#: six characters, which pushes these assertions past the line limit for no
#: reason. One Arabic letter is one character here and two bytes on the wire,
#: and that difference is what the clamping tests below are about.
ARABIC_LETTER = "ا"
COMMENT_TEXT = "كم سعر التشطيب؟"
REEL_COMMENT_TEXT = "عايز عرض سعر"
PUBLIC_REPLY = "شكراً لتواصلك"
GREETING = "أهلاً"
INVITE = "رسالة خاصة"
OVERRIDE = "نص بديل"


def _settings(**overrides: Any) -> ChannelSettings:
    """Channel settings with the Instagram credentials and the linked page.

    Defaults are merged rather than splatted alongside ``**overrides``: a test
    that overrides one of the pinned keys would otherwise raise TypeError for
    a duplicate keyword argument at call time, which mypy does not catch here
    because tests are excluded from it.
    """
    values: dict[str, Any] = {
        "enable_instagram_comments": True,
        "instagram_account_id": IG_ACCOUNT,
        "facebook_page_id": PAGE_ID,
        "facebook_page_access_token": "unit-test-placeholder",
    }
    values.update(overrides)
    return ChannelSettings(_env_file=None, **values)


def _adapter(
    status: int = 200,
    body: dict[str, Any] | None = None,
    **overrides: Any,
) -> tuple[InstagramCommentAdapter, list[httpx.Request]]:
    """A real adapter whose transport records requests instead of sending them."""
    adapter = InstagramCommentAdapter(_settings(**overrides))
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
    """The Facebook Login for Business shape: nested changes, ``comment_id``."""
    value: dict[str, Any] = {
        "from": {"id": COMMENTER, "username": "customer"},
        "comment_id": COMMENT_ID,
        "text": COMMENT_TEXT,
        "media": {"id": MEDIA_ID, "media_product_type": "FEED"},
    }
    value.update(value_overrides)
    return {
        "object": "instagram",
        "entry": [
            {
                "id": IG_ACCOUNT,
                "time": 1735689600,
                "changes": [{"field": "comments", "value": value}],
            }
        ],
    }


def _instagram_login_delivery(**value_overrides: Any) -> dict[str, Any]:
    """The Instagram Login shape: field and value on the entry, key ``id``."""
    value: dict[str, Any] = {
        "from": {"id": COMMENTER, "username": "customer"},
        "id": COMMENT_ID,
        "text": REEL_COMMENT_TEXT,
        "media": {"id": MEDIA_ID, "media_product_type": "REELS"},
    }
    value.update(value_overrides)
    return {
        "object": "instagram",
        "entry": [{"id": IG_ACCOUNT, "field": "comments", "value": value}],
    }


# --- Inbound normalisation --------------------------------------------------


def test_a_customer_comment_becomes_one_text_event() -> None:
    adapter, _ = _adapter()
    events = list(adapter.parse(_delivery()))

    assert len(events) == 1
    event = events[0]
    assert event.channel == INSTAGRAM_COMMENT
    assert event.kind == EVENT_TEXT
    assert event.sender_id == COMMENTER
    assert event.sender_name == "customer"
    assert event.text == COMMENT_TEXT


def test_the_comment_id_is_the_provider_message_id() -> None:
    """It is the pipeline's idempotency key, so it must be the comment's id.

    claim_inbound dedupes on this value, which is what makes the duplicate
    notifications Meta documents for boosted and ads posts collapse into one
    stored comment.
    """
    adapter, _ = _adapter()
    (event,) = list(adapter.parse(_delivery()))

    assert event.provider_message_id == COMMENT_ID


def test_the_instagram_login_shape_is_accepted_too() -> None:
    """The regression that would drop a whole authentication mode in silence.

    Instagram Login puts field and value straight on the entry and names the
    comment ``id``. An adapter that reads only ``changes[].value.comment_id``
    parses nothing, raises nothing, and answers 200 -- so the comments just
    never arrive and no error anywhere says why.
    """
    adapter, _ = _adapter()
    (event,) = list(adapter.parse(_instagram_login_delivery()))

    assert event.provider_message_id == COMMENT_ID
    assert event.kind == EVENT_TEXT
    assert event.text == REEL_COMMENT_TEXT


def test_the_facebook_login_key_wins_when_a_payload_carries_both() -> None:
    """``id`` is not documented to be the comment in the nested shape."""
    adapter, _ = _adapter()
    (event,) = list(adapter.parse(_delivery(id="not-the-comment")))

    assert event.provider_message_id == COMMENT_ID


def test_the_thread_coordinates_travel_with_the_event() -> None:
    adapter, _ = _adapter()
    (event,) = list(adapter.parse(_delivery(parent_id="17900000000000000")))

    assert event.context["comment_id"] == COMMENT_ID
    assert event.context["account_id"] == IG_ACCOUNT
    assert event.context["parent_id"] == "17900000000000000"
    assert event.context["media_id"] == MEDIA_ID
    assert event.context["media_product_type"] == "FEED"


def test_a_boosted_posts_ad_details_travel_with_the_event() -> None:
    """Meta documents these as the cause of duplicate notifications."""
    adapter, _ = _adapter()
    media = {"id": MEDIA_ID, "ad_id": "120000000000001", "ad_title": "Spring"}
    (event,) = list(adapter.parse(_delivery(media=media)))

    assert event.context["ad_id"] == "120000000000001"
    assert event.context["ad_title"] == "Spring"


def test_no_timestamp_is_invented_when_the_payload_carries_none() -> None:
    """The documented comments payload has no created_time. None is made up.

    Fabricating one from entry[].time would be a guess about an undocumented
    unit, and guessing a unit is exactly what backdates every comment to 1970
    on the Page surface. None reads as fresh, so comments still get answered.
    """
    adapter, _ = _adapter()
    (event,) = list(adapter.parse(_delivery()))

    assert event.sent_at is None
    assert event.age is None


def test_a_comment_with_no_text_is_unsupported_rather_than_dropped() -> None:
    """A sticker-only comment still deserves an answer, not silence."""
    adapter, _ = _adapter()
    (event,) = list(adapter.parse(_delivery(text="")))

    assert event.kind == EVENT_UNSUPPORTED
    assert event.provider_message_id == COMMENT_ID


# --- The guards -------------------------------------------------------------


def test_the_accounts_own_comment_is_ignored() -> None:
    """Otherwise the bot answers itself, and every answer is another webhook."""
    adapter, _ = _adapter()
    delivery = _delivery(**{"from": {"id": IG_ACCOUNT, "username": "us"}})

    assert list(adapter.parse(delivery)) == []


def test_the_accounts_own_comment_is_kept_when_the_guard_is_switched_off() -> None:
    adapter, _ = _adapter(ignore_own_comments=False)
    delivery = _delivery(**{"from": {"id": IG_ACCOUNT, "username": "us"}})

    assert len(list(adapter.parse(delivery))) == 1


def test_the_delivery_decides_ownership_when_nothing_is_configured() -> None:
    """A token moved between accounts leaves the configured id briefly wrong.

    The delivery is the one telling the truth about which account produced
    this comment, so it is checked as well as the configured id.
    """
    adapter, _ = _adapter(instagram_account_id="")
    delivery = _delivery(**{"from": {"id": IG_ACCOUNT, "username": "us"}})

    assert list(adapter.parse(delivery)) == []


def test_a_comment_without_an_id_is_dropped() -> None:
    adapter, _ = _adapter()

    assert list(adapter.parse(_delivery(comment_id=""))) == []


def test_an_anonymous_comment_is_dropped_rather_than_half_routed() -> None:
    """No author means no user to resolve and nobody to answer privately."""
    adapter, _ = _adapter()

    assert list(adapter.parse(_delivery(**{"from": {}}))) == []


def test_a_live_comment_is_not_treated_as_an_ordinary_comment() -> None:
    """A private reply to a live comment only works during the broadcast.

    Answering one as though it were an ordinary comment would promise a
    follow-up that can no longer be delivered by the time it is attempted.
    """
    adapter, _ = _adapter()
    delivery = _delivery()
    delivery["entry"][0]["changes"][0]["field"] = "live_comments"

    assert list(adapter.parse(delivery)) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"object": "instagram"},
        {"object": "instagram", "entry": None},
        {"object": "instagram", "entry": ["not-an-entry"]},
        {"object": "instagram", "entry": [{"id": IG_ACCOUNT}]},
        {"object": "instagram", "entry": [{"id": IG_ACCOUNT, "changes": None}]},
        {"object": "instagram", "entry": [{"id": IG_ACCOUNT, "changes": ["no"]}]},
        {"object": "instagram", "entry": [{"id": IG_ACCOUNT, "field": "mentions"}]},
        {"object": "instagram", "entry": [{"field": "comments", "value": "text"}]},
        {
            "object": "instagram",
            "entry": [{"id": IG_ACCOUNT, "changes": [{"field": "comments"}]}],
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
    second = _delivery(comment_id=COMMENT_ID + "2", text=PUBLIC_REPLY)
    first["entry"].append(second["entry"][0])

    events = list(adapter.parse(first))

    assert [event.provider_message_id for event in events] == [
        COMMENT_ID,
        COMMENT_ID + "2",
    ]


def test_both_envelope_shapes_can_arrive_in_one_delivery() -> None:
    """Nothing documents them as mutually exclusive within a delivery."""
    adapter, _ = _adapter()
    delivery = _delivery()
    second = _instagram_login_delivery(id=COMMENT_ID + "2")
    delivery["entry"].append(second["entry"][0])

    events = list(adapter.parse(delivery))

    assert [event.provider_message_id for event in events] == [
        COMMENT_ID,
        COMMENT_ID + "2",
    ]


# --- Outbound ---------------------------------------------------------------


async def test_a_public_reply_posts_to_the_replies_edge() -> None:
    """``message`` is documented as a query string parameter on this edge."""
    adapter, seen = _adapter()

    await adapter.reply_to_comment(COMMENT_ID, PUBLIC_REPLY)

    assert len(seen) == 1
    assert seen[0].url.path.endswith("/" + COMMENT_ID + "/replies")
    assert seen[0].url.params["message"] == PUBLIC_REPLY
    # Sent as documented: a parameter, not a JSON body.
    assert seen[0].content == b""


async def test_send_text_is_a_public_reply_addressed_to_the_comment() -> None:
    """send_text is ChatService's sender contract; recipient is a comment id."""
    adapter, seen = _adapter()

    await adapter.send_text(COMMENT_ID, GREETING)

    assert len(seen) == 1
    assert seen[0].url.path.endswith("/" + COMMENT_ID + "/replies")


async def test_a_private_reply_goes_to_the_linked_page_not_the_account() -> None:
    """Meta documents /<PAGE_ID>/messages for Instagram private replies.

    Not the Instagram account id, and not the /me/messages path Instagram DM
    sends use. Addressing either of those is a 400 per invitation, with the
    customer having already been answered in public and never followed up.
    """
    adapter, seen = _adapter(body={"recipient_id": COMMENTER, "message_id": "m1"})

    result = await adapter.invite_to_private_thread(COMMENT_ID, INVITE)

    assert len(seen) == 1
    assert seen[0].url.path.endswith("/" + PAGE_ID + "/messages")
    assert IG_ACCOUNT not in str(seen[0].url)
    body = json.loads(seen[0].content)
    assert body["recipient"] == {"comment_id": COMMENT_ID}
    assert body["message"] == {"text": INVITE}
    # Not part of the private reply contract.
    assert "messaging_type" not in body
    # The returned IGSID is the comment-to-DM link every conversion metric needs.
    assert result["recipient_id"] == COMMENTER


async def test_a_private_reply_without_a_page_id_fails_loudly() -> None:
    """This channel's own credentials are not enough to send an invitation.

    The Instagram pair satisfies the registry, because a deployment that only
    answers publicly needs nothing more. An invitation additionally needs the
    linked page, and saying so beats a POST to "//messages".
    """
    adapter, seen = _adapter(facebook_page_id="")

    with pytest.raises(ExternalServiceError):
        await adapter.invite_to_private_thread(COMMENT_ID, INVITE)

    assert seen == []


async def test_a_refused_private_reply_becomes_an_external_service_error() -> None:
    """One private reply per commenter: the second is a normal refusal."""
    adapter, _ = _adapter(status=400)

    with pytest.raises(ExternalServiceError):
        await adapter.invite_to_private_thread(COMMENT_ID, INVITE)


async def test_a_refused_public_reply_becomes_an_external_service_error() -> None:
    adapter, _ = _adapter(status=400)

    with pytest.raises(ExternalServiceError):
        await adapter.reply_to_comment(COMMENT_ID, GREETING)


async def test_a_long_private_reply_is_clamped_in_bytes_not_characters() -> None:
    """Instagram states its message limit in BYTES, and Arabic is two each.

    A character-based clamp would send twice the documented limit and take a
    400 on a real customer's invitation.
    """
    adapter, seen = _adapter()

    await adapter.invite_to_private_thread(COMMENT_ID, ARABIC_LETTER * 5000)

    text = json.loads(seen[0].content)["message"]["text"]
    assert len(text.encode("utf-8")) == 1000
    assert len(text) == 500


async def test_a_long_public_reply_is_clamped_rather_than_rejected() -> None:
    adapter, seen = _adapter()

    await adapter.reply_to_comment(COMMENT_ID, ARABIC_LETTER * 5000)

    message = seen[0].url.params["message"]
    assert len(message.encode("utf-8")) == 2000


def test_the_arabic_fixture_really_is_two_bytes() -> None:
    """Guards the two clamping tests above against a silent fixture change.

    If this letter were ever replaced with an ASCII one, both byte assertions
    would still pass while testing nothing about multi-byte text.
    """
    assert len(ARABIC_LETTER) == 1
    assert len(ARABIC_LETTER.encode("utf-8")) == 2


# --- Wiring -----------------------------------------------------------------


def test_the_adapter_registers_itself_for_its_own_channel() -> None:
    """Registration is the entire wiring a comment channel needs.

    ``outbound_adapter`` and ``meta_inbound_adapters`` both resolve through the
    registry, and neither grew a comment branch for this channel.
    """
    assert InstagramCommentAdapter.channel == INSTAGRAM_COMMENT
    assert issubclass(InstagramCommentAdapter, CommentChannelAdapter)
    assert registry.adapter_class(INSTAGRAM_COMMENT) is InstagramCommentAdapter


# --- The copy is configuration, not code ------------------------------------


def test_the_invitation_copy_comes_from_settings_not_the_adapter() -> None:
    """The adapter takes the text as a parameter and holds no copy of its own.

    The default is temporary and unreviewed on purpose; what this pins is that
    replacing it is a settings change rather than a code change.
    """
    assert _settings().dm_invite_message(INSTAGRAM_COMMENT) == (
        DEFAULT_INSTAGRAM_COMMENT_DM_INVITE
    )

    overridden = _settings(instagram_comment_dm_invite_message=OVERRIDE)
    assert overridden.dm_invite_message(INSTAGRAM_COMMENT) == OVERRIDE


def test_a_blank_override_falls_back_instead_of_sending_nothing() -> None:
    """A deployer who copies .env.example must not send an empty message."""
    blank = _settings(instagram_comment_dm_invite_message="   ")

    assert blank.dm_invite_message(INSTAGRAM_COMMENT) == (
        DEFAULT_INSTAGRAM_COMMENT_DM_INVITE
    )


def test_the_two_comment_surfaces_keep_separate_copy() -> None:
    """Identical wording today, and separately overridable, which is the point.

    A comment under a Reel and a comment under a Facebook post are different
    audiences. Sharing one string now would make separating them later a code
    change instead of a settings change.
    """
    settings = _settings(instagram_comment_dm_invite_message=OVERRIDE)

    assert settings.dm_invite_message(INSTAGRAM_COMMENT) == OVERRIDE
    assert settings.dm_invite_message(FACEBOOK_COMMENT) == (
        DEFAULT_FACEBOOK_COMMENT_DM_INVITE
    )
