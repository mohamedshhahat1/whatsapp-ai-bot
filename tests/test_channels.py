"""The channel layer: ids, capabilities, normalisation, switches.

Weighted towards the failures that would otherwise be silent -- a renamed id
that splits a channel's history, a comment channel that grows a session, a
switch turned on without the credentials behind it.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.channels import constants, registry
from app.channels.base import BaseChannelAdapter
from app.channels.config import ChannelSettings
from app.channels.constants import (
    ALL_CHANNELS,
    FACEBOOK_COMMENT,
    INSTAGRAM_COMMENT,
    INSTAGRAM_DM,
    MESSENGER,
    PUBLIC_CHANNELS,
    WHATSAPP,
)
from app.channels.events import (
    EVENT_MEDIA,
    EVENT_SELECTION,
    EVENT_TEXT,
    EVENT_UNSUPPORTED,
    InboundEvent,
)


def _settings(**overrides: Any) -> ChannelSettings:
    """Settings built from explicit values only.

    ``_env_file=None`` keeps a developer's local .env from deciding whether
    these assertions pass.
    """
    return ChannelSettings(_env_file=None, **overrides)


# --- Channel ids and profiles ---------------------------------------------


def test_channel_ids_are_what_the_database_already_holds():
    """Literal ids, because a rename does not migrate the rows already written.

    These strings go into conversations.channel, get filtered on by the
    dashboard and the Flutter app, and get grouped by in analytics. Rewording
    one splits that channel's history in two and nothing complains. If you are
    changing a line here, you need a migration, not an edit.
    """
    assert WHATSAPP == "whatsapp"
    assert MESSENGER == "messenger"
    assert INSTAGRAM_DM == "instagram_dm"
    assert FACEBOOK_COMMENT == "facebook_comment"
    assert INSTAGRAM_COMMENT == "instagram_comment"


def test_every_profile_agrees_with_the_key_it_is_filed_under():
    for channel, profile in constants.PROFILES.items():
        assert profile.id == channel
        assert profile.label
        assert profile.icon


def test_no_id_outgrows_the_column_that_will_store_it():
    for channel in ALL_CHANNELS:
        assert len(channel) <= constants.CHANNEL_ID_MAX


def test_public_channels_have_no_session():
    """A comment thread has nobody to greet and no session to time out.

    Running the lifecycle over a public thread would greet a comment section,
    close it on an idle timer, and count both in the session analytics.
    """
    for channel in PUBLIC_CHANNELS:
        assert constants.profile(channel).has_session is False
        assert constants.profile(channel).supports_quick_replies is False


def test_only_public_channels_continue_somewhere_private():
    for channel in ALL_CHANNELS:
        sibling = constants.private_sibling(channel)
        if channel in PUBLIC_CHANNELS:
            assert sibling in constants.PRIVATE_CHANNELS
        else:
            assert sibling is None


def test_comments_point_at_the_right_inbox():
    assert constants.private_sibling(FACEBOOK_COMMENT) == MESSENGER
    assert constants.private_sibling(INSTAGRAM_COMMENT) == INSTAGRAM_DM


def test_an_unknown_channel_is_loud():
    """Better a raise in the worker than a customer answered on the wrong app."""
    assert constants.is_known("telegram") is False
    with pytest.raises(ValueError):
        constants.profile("telegram")


# --- Normalised events ------------------------------------------------------


def _event(**overrides: Any) -> InboundEvent:
    base = {
        "channel": WHATSAPP,
        "sender_id": "20100000000",
        "provider_message_id": "wamid.TEST",
        "kind": EVENT_TEXT,
        "text": "\\u0639\\u0627\\u064a\\u0632 \\u0623\\u0639\\u0631\\u0641 \\u0627\\u0644\\u0623\\u0633\\u0639\\u0627\\u0631",
    }
    return InboundEvent(**{**base, **overrides})


def test_body_is_defined_once_per_kind():
    """The transcript, the dashboard and a stale delivery must agree."""
    assert (
        _event().body
        == "\\u0639\\u0627\\u064a\\u0632 \\u0623\\u0639\\u0631\\u0641 \\u0627\\u0644\\u0623\\u0633\\u0639\\u0627\\u0631"
    )
    assert (
        _event(
            kind=EVENT_SELECTION, selection_id="request_quote", selection_title="Quote"
        ).body
        == "Quote"
    )
    assert _event(kind=EVENT_MEDIA, media_type="image").body == "[image received]"
    assert _event(kind=EVENT_UNSUPPORTED).body == "[unsupported message received]"


def test_a_selection_falls_back_to_its_id():
    """Some platforms return the tapped id without the label."""
    event = _event(kind=EVENT_SELECTION, selection_id="request_quote")
    assert event.body == "request_quote"


def test_an_event_without_ids_is_not_routable():
    """No sender, nobody to answer. No message id, no idempotency guard."""
    assert _event().routable is True
    assert _event(sender_id="").routable is False
    assert _event(provider_message_id="").routable is False


def test_a_missing_timestamp_reads_as_fresh():
    """Fail open, exactly like webhook_processor._message_age.

    A change to Meta's timestamp format must not silence every reply the bot
    makes.
    """
    assert _event().age is None


def test_age_measures_from_when_the_customer_sent_it():
    sent = datetime.now(UTC) - timedelta(minutes=42)
    age = _event(sent_at=sent).age
    assert age is not None
    assert timedelta(minutes=41) < age < timedelta(minutes=43)


def test_context_is_per_event():
    """A shared default dict would leak one comment's ids into the next."""
    first, second = _event(), _event()
    first.context["comment_id"] = "1"
    assert second.context == {}


# --- Switches and readiness -------------------------------------------------


def test_new_channels_ship_switched_off():
    """Read off the declared defaults, so the environment cannot decide this.

    Deploying must change nothing until somebody sets a flag: the WhatsApp bot
    is answering real customers, and a channel that woke up at deploy time
    would start answering on a page whose copy nobody had reviewed.
    """
    fields = ChannelSettings.model_fields
    assert fields["enable_whatsapp"].default is True
    for name in (
        "enable_messenger",
        "enable_instagram_dm",
        "enable_facebook_comments",
        "enable_instagram_comments",
    ):
        assert fields[name].default is False, name


def test_dm_invitations_are_off_until_asked_for():
    fields = ChannelSettings.model_fields
    assert fields["facebook_comment_dm_invite"].default is False
    assert fields["instagram_comment_dm_invite"].default is False


def test_channels_switch_independently():
    settings = _settings(enable_whatsapp=False, enable_messenger=True)
    assert registry.enabled_channels(settings) == {MESSENGER}
    assert registry.is_enabled(WHATSAPP, settings) is False


def test_meta_credentials_fall_back_to_the_whatsapp_app():
    """One Meta app signs every surface; two copies of a secret drift apart."""
    settings = _settings()
    assert settings.verify_token("wa-verify") == "wa-verify"
    assert settings.app_secret("wa-secret") == "wa-secret"


def test_an_explicit_meta_credential_wins():
    settings = _settings(facebook_verify_token="fb-verify", meta_app_secret="fb-secret")
    assert settings.verify_token("wa-verify") == "fb-verify"
    assert settings.app_secret("wa-secret") == "fb-secret"


def test_instagram_borrows_the_page_token_when_it_has_none():
    settings = _settings(
        enable_instagram_dm=True,
        instagram_account_id="ig-1",
        facebook_page_access_token="page-token",
    )
    assert settings.instagram_token() == "page-token"
    assert registry.missing_credentials(INSTAGRAM_DM, settings) == ()


def test_a_channel_switched_on_without_credentials_is_reported_not_used():
    """The alternative is finding out from a 400, with a customer waiting."""
    settings = _settings(enable_messenger=True)
    assert registry.readiness(settings) == {
        MESSENGER: ("facebook_page_id", "facebook_page_access_token")
    }
    assert MESSENGER not in registry.usable_channels(settings)
    assert WHATSAPP in registry.usable_channels(settings)


def test_a_fully_configured_channel_is_usable_and_silent():
    settings = _settings(
        enable_messenger=True,
        facebook_page_id="page-1",
        facebook_page_access_token="page-token",
    )
    assert registry.readiness(settings) == {}
    assert registry.usable_channels(settings) == {WHATSAPP, MESSENGER}


def test_whatsapp_credentials_are_not_checked_twice():
    """Settings.REQUIRED_IN_PRODUCTION already owns them."""
    assert registry.REQUIRED_CREDENTIALS[WHATSAPP] == ()


# --- Adapter contract -------------------------------------------------------


class _FakeAdapter(BaseChannelAdapter):
    """Records what left, so the degradation path can be observed."""

    channel = FACEBOOK_COMMENT

    def __init__(self) -> None:
        self.sent: list[str] = []

    def parse(self, payload: dict[str, Any]):
        return []

    async def send_text(self, recipient: str, text: str) -> dict[str, Any]:
        self.sent.append(text)
        return {"recipient": recipient}


class _LyingAdapter(_FakeAdapter):
    """Claims a capability it never implemented."""

    channel = WHATSAPP


async def test_quick_replies_degrade_to_text_where_unsupported():
    """An answered customer beats a pretty one."""
    adapter = _FakeAdapter()
    result = await adapter.send_quick_replies(
        "psid-1",
        "\\u0634\\u0643\\u0631\\u0627\\u064b \\u0644\\u062a\\u0648\\u0627\\u0635\\u0644\\u0643",
        [("request_quote", "Quote")],
    )
    assert adapter.sent == [
        "\\u0634\\u0643\\u0631\\u0627\\u064b \\u0644\\u062a\\u0648\\u0627\\u0635\\u0644\\u0643"
    ]
    assert result == {"recipient": "psid-1"}


async def test_claiming_quick_replies_without_implementing_them_is_an_error():
    """Silently dropping the buttons would look like a copy bug for weeks."""
    with pytest.raises(NotImplementedError):
        await _LyingAdapter().send_quick_replies("20100000000", "hi", [("a", "A")])


def test_an_adapter_exposes_its_own_capabilities():
    assert _FakeAdapter().profile.private is False
    assert _LyingAdapter().profile.supports_media_out is True


def test_the_registry_refuses_an_adapter_with_no_real_channel():
    class Nowhere(_FakeAdapter):
        channel = "myspace"

    with pytest.raises(ValueError):
        registry.register_adapter(Nowhere)


def test_a_registered_adapter_can_be_found_again():
    registry.register_adapter(_FakeAdapter)
    assert registry.adapter_class(FACEBOOK_COMMENT) is _FakeAdapter
