"""Outbound routing: an operator reply leaves on the customer's own channel.

ReplyService used to hold a WhatsAppClient and refuse everyone else, so the
tests only had to prove one transport worked. The transport is now chosen
from ``conversations.channel``, which moves the interesting failures
somewhere no existing test looks.

Everything here is database-free. The routing decision is a pure function of
(channel, settings, registry), and that is exactly what makes it worth
testing directly: the route-level tests in test_reply_window.py can only
reach it through a customer fixture, and that fixture can only be WhatsApp.
"""

import inspect
from typing import Any

import pytest

from app.channels import registry
from app.channels.config import ChannelSettings
from app.channels.constants import ALL_CHANNELS, INSTAGRAM_DM, MESSENGER, WHATSAPP
from app.channels.messenger import MessengerAdapter
from app.channels.outbound import (
    ChannelUnavailableError,
    outbound_adapter,
    provider_message_id,
    recipient_id,
)
from app.channels.whatsapp import WhatsAppAdapter
from app.config import get_settings
from app.core.exceptions import ConflictError
from app.integrations.whatsapp import WhatsAppClient
from app.models.user import User
from app.services.reply_service import ReplyService, UnsupportedChannelError

#: Channels this repository can actually send on today. The rest are real ids
#: with real profiles and no adapter yet, and they must refuse rather than
#: quietly fall back to WhatsApp.
IMPLEMENTED = (WHATSAPP, MESSENGER)


def _settings(**overrides: Any) -> ChannelSettings:
    """Settings built from explicit values only.

    ``_env_file=None`` keeps a developer's local .env from deciding whether
    these assertions pass.
    """
    return ChannelSettings(_env_file=None, **overrides)


def _everything_on() -> ChannelSettings:
    """Every channel switched on and fully credentialled.

    With the switches and the credentials both satisfied, the only remaining
    question is whether an adapter exists -- which is the question the
    per-channel test is asking.
    """
    return _settings(
        enable_whatsapp=True,
        enable_messenger=True,
        enable_instagram_dm=True,
        enable_facebook_comments=True,
        enable_instagram_comments=True,
        facebook_page_id="page-1",
        facebook_page_access_token="page-token",
        instagram_account_id="ig-1",
    )


@pytest.fixture
def whatsapp_client() -> WhatsAppClient:
    """A real client that never reaches the network.

    Real rather than a stub because the adapter is meant to hand the
    process-wide singleton straight through, and identity is the thing worth
    asserting. No connection is ever opened -- nothing here calls ``_post``
    without replacing it first -- so there is nothing to close.
    """
    return WhatsAppClient(get_settings())


@pytest.fixture
def only_real_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the registry to the adapters this repository ships.

    tests/test_channels.py registers a fake adapter for facebook_comment into
    the module-level mapping and never removes it, so whether that channel
    looks implemented depends on which file pytest ran first. That fake's
    ``__init__`` takes no arguments, so a polluted registry would surface here
    as a TypeError from the wrong construction path rather than as an honest
    assertion failure.
    """
    monkeypatch.setattr(
        registry,
        "_ADAPTERS",
        {WHATSAPP: WhatsAppAdapter, MESSENGER: MessengerAdapter},
    )


# --- Provider message ids ---------------------------------------------------


def test_the_whatsapp_envelope_yields_its_message_id() -> None:
    response = {"messages": [{"id": "wamid.HBgLMjAxMDAwMDAwMDA="}]}
    assert provider_message_id(response) == "wamid.HBgLMjAxMDAwMDAwMDA="


def test_the_messenger_envelope_yields_its_message_id() -> None:
    """The shape the old inline lookup could not see.

    ReplyService read ``response["messages"][0]["id"]`` directly, so every
    Messenger reply would have stored NULL -- and the delivery-status and
    idempotency paths both key on that column.
    """
    assert provider_message_id({"message_id": "mid.abc123"}) == "mid.abc123"


def test_whatsapp_wins_when_both_shapes_somehow_arrive() -> None:
    """Only one platform can have sent it, and WhatsApp's is the specific key."""
    response = {"messages": [{"id": "wamid.X"}], "message_id": "mid.Y"}
    assert provider_message_id(response) == "wamid.X"


def test_a_numeric_id_is_still_stored_as_text() -> None:
    """``messages.wa_message_id`` is a string column, so a numeric id from a
    future API version must become "12345" rather than None."""
    assert provider_message_id({"message_id": 12345}) == "12345"


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"messages": []},
        {"messages": [{}]},
        {"messages": [{"id": ""}]},
        {"messages": "not-a-list"},
        {"message_id": ""},
        {"message_id": None},
    ],
)
def test_an_unreadable_envelope_is_none_rather_than_a_crash(
    response: dict[str, Any],
) -> None:
    """A send that already succeeded must not be undone by its receipt.

    By the time this runs the message is on the customer's phone. Raising
    would roll the transaction back and lose the transcript entry for a
    message that was genuinely delivered.
    """
    assert provider_message_id(response) is None


# --- Addressing a customer --------------------------------------------------


def test_a_messenger_customer_is_addressed_by_their_external_id() -> None:
    user = User(channel=MESSENGER, external_id="psid-1", wa_id=None)
    assert recipient_id(user) == "psid-1"


def test_a_whatsapp_row_written_before_0013_still_resolves() -> None:
    """Rows from the expand phase carry a phone number and no external_id."""
    user = User(channel=WHATSAPP, external_id=None, wa_id="20100000000")
    assert recipient_id(user) == "20100000000"


def test_external_id_wins_when_a_row_carries_both() -> None:
    user = User(channel=WHATSAPP, external_id="20100000000", wa_id="20100000000")
    assert recipient_id(user) == "20100000000"


def test_a_customer_with_no_id_at_all_is_unaddressable() -> None:
    """Returning "" here would send a reply to nobody and report success."""
    assert recipient_id(User(channel=WHATSAPP)) is None


# --- Routing to the right adapter -------------------------------------------


def test_whatsapp_routes_to_the_whatsapp_adapter(
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
) -> None:
    adapter = outbound_adapter(
        WHATSAPP,
        whatsapp_client=whatsapp_client,
        settings=_settings(),
    )
    assert isinstance(adapter, WhatsAppAdapter)
    assert adapter.channel == WHATSAPP


def test_the_whatsapp_adapter_reuses_the_shared_client(
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
) -> None:
    """The same object, not a copy and not a second pool.

    ``deps.get_whatsapp_client`` is an lru_cache singleton holding one httpx
    connection pool. Building a client from Settings inside the adapter would
    open a fresh pool on every operator reply, and nothing would fail loudly.
    """
    adapter = outbound_adapter(
        WHATSAPP,
        whatsapp_client=whatsapp_client,
        settings=_settings(),
    )
    assert adapter._client is whatsapp_client


async def test_messenger_routes_to_the_messenger_adapter(
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
) -> None:
    adapter = outbound_adapter(
        MESSENGER,
        whatsapp_client=whatsapp_client,
        settings=_everything_on(),
    )
    try:
        assert isinstance(adapter, MessengerAdapter)
        assert adapter.channel == MESSENGER
    finally:
        await adapter.aclose()


async def test_messenger_is_not_built_out_of_the_whatsapp_client(
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
) -> None:
    """The one construction difference, asserted rather than assumed.

    WhatsApp reuses the shared client; every other channel builds its own
    transport from channel settings. Handing the WhatsApp client to Messenger
    would post page messages with the WhatsApp token and a phone-number id in
    the URL, which is a 400 per reply rather than a crash.
    """
    settings = _everything_on()
    adapter = outbound_adapter(
        MESSENGER,
        whatsapp_client=whatsapp_client,
        settings=settings,
    )
    try:
        assert adapter._settings is settings
        assert adapter._client is not whatsapp_client._client
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("channel", sorted(ALL_CHANNELS))
async def test_every_supported_channel_either_routes_or_refuses(
    channel: str,
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
) -> None:
    """No channel is allowed to half-work.

    Every id in ALL_CHANNELS either resolves to an adapter that reports the
    same id back, or refuses with a 409 an operator can read. The regression
    this catches is a channel added to constants.py and wired nowhere: without
    it, the first operator reply is what discovers the gap.
    """
    settings = _everything_on()

    if channel not in IMPLEMENTED:
        with pytest.raises(ChannelUnavailableError) as refusal:
            outbound_adapter(
                channel,
                whatsapp_client=whatsapp_client,
                settings=settings,
            )
        assert "adapter" in str(refusal.value)
        return

    adapter = outbound_adapter(
        channel,
        whatsapp_client=whatsapp_client,
        settings=settings,
    )
    try:
        assert adapter.channel == channel
    finally:
        await adapter.aclose()


# --- Refusals ---------------------------------------------------------------


def test_an_unknown_channel_is_refused(whatsapp_client: WhatsAppClient) -> None:
    """A conversation row holding a typo must not resolve to anything."""
    with pytest.raises(ChannelUnavailableError) as refusal:
        outbound_adapter(
            "telegram",
            whatsapp_client=whatsapp_client,
            settings=_settings(),
        )
    assert "Unknown channel" in str(refusal.value)


def test_a_switched_off_channel_is_refused(whatsapp_client: WhatsAppClient) -> None:
    """The default deployment: Messenger ships off."""
    with pytest.raises(ChannelUnavailableError) as refusal:
        outbound_adapter(
            MESSENGER,
            whatsapp_client=whatsapp_client,
            settings=_settings(enable_messenger=False),
        )
    assert "switched off" in str(refusal.value)


def test_a_channel_enabled_without_credentials_is_refused(
    whatsapp_client: WhatsAppClient,
) -> None:
    """Enabled is not ready.

    Left to the Graph API this is a 400 with a customer waiting, surfaced to
    the operator as a 500 for what is really a deployment mistake.
    """
    with pytest.raises(ChannelUnavailableError) as refusal:
        outbound_adapter(
            MESSENGER,
            whatsapp_client=whatsapp_client,
            settings=_settings(enable_messenger=True),
        )
    assert "not configured" in str(refusal.value)


def test_a_known_channel_with_no_adapter_yet_is_refused(
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
) -> None:
    """instagram_dm has an id, a profile, a switch and credentials -- and no
    adapter. Falling back to WhatsApp here would answer an Instagram customer
    on a phone number they never gave us."""
    with pytest.raises(ChannelUnavailableError) as refusal:
        outbound_adapter(
            INSTAGRAM_DM,
            whatsapp_client=whatsapp_client,
            settings=_everything_on(),
        )
    assert "adapter" in str(refusal.value)


def test_channel_unavailable_is_a_conflict_not_a_server_error() -> None:
    """Nothing is broken in any of the three cases it covers.

    A switch is off, or credentials are missing, or the channel has no adapter
    yet. All three are deployment states an operator can be told about, and a
    500 would put them in the error rate that alerting watches.
    """
    assert issubclass(ChannelUnavailableError, ConflictError)
    assert ChannelUnavailableError.status_code == 409
    assert ChannelUnavailableError.code == "channel_unavailable"


def test_the_unsupported_channel_code_is_unchanged() -> None:
    """Both clients branch on this string.

    The class now means "this customer has no id on record" rather than "we
    only do WhatsApp", but renaming the code would be a client-visible change
    for no gain.
    """
    assert issubclass(UnsupportedChannelError, ConflictError)
    assert UnsupportedChannelError.status_code == 409
    assert UnsupportedChannelError.code == "unsupported_channel"


# --- The whole journey, with the network replaced ---------------------------


async def test_a_whatsapp_send_reaches_the_client_and_yields_its_id(
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter to client to envelope to provider id, cut at the single seam."""
    sent: list[dict[str, Any]] = []

    async def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return {"messages": [{"id": "wamid.SENT"}]}

    monkeypatch.setattr(whatsapp_client, "_post", fake_post)
    adapter = outbound_adapter(
        WHATSAPP,
        whatsapp_client=whatsapp_client,
        settings=_settings(),
    )

    response = await adapter.send_text("20100000000", "Hello")

    assert sent[0]["to"] == "20100000000"
    assert sent[0]["text"]["body"] == "Hello"
    assert provider_message_id(response) == "wamid.SENT"


async def test_a_messenger_send_reaches_its_own_client_and_yields_its_id(
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same journey on the other transport, ending in the other envelope.

    This pair is the one that used to store NULL: routing worked, the customer
    got the message, and the id never landed.
    """
    sent: list[dict[str, Any]] = []

    async def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return {"recipient_id": "psid-1", "message_id": "mid.SENT"}

    adapter = outbound_adapter(
        MESSENGER,
        whatsapp_client=whatsapp_client,
        settings=_everything_on(),
    )
    try:
        monkeypatch.setattr(adapter, "_post", fake_post)
        response = await adapter.send_text("psid-1", "Hello")
    finally:
        await adapter.aclose()

    assert sent[0]["recipient"]["id"] == "psid-1"
    assert sent[0]["message"]["text"] == "Hello"
    assert sent[0]["messaging_type"] == "RESPONSE"
    assert provider_message_id(response) == "mid.SENT"


def test_the_reply_path_asks_for_an_adapter_rather_than_a_client() -> None:
    """A source-level guard, because the regression it catches is silent.

    If send_manual_reply ever goes back to calling ``self._whatsapp.send_text``
    directly, every non-WhatsApp reply is delivered on the wrong transport or
    not at all -- and every existing reply test still passes, because they are
    all WhatsApp. The honest version of this test needs a Messenger customer
    in the database, which conftest cannot currently build: every cleanup
    statement there keys on wa_id, and a Messenger row does not have one.
    """
    source = inspect.getsource(ReplyService.send_manual_reply)
    assert "outbound_adapter(" in source
    assert "adapter.send_text(" in source
    assert "self._whatsapp.send_text" not in source
