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

from app.channels import outbound, registry
from app.channels.base import CommentChannelAdapter
from app.channels.config import ChannelSettings
from app.channels.constants import (
    ALL_CHANNELS,
    FACEBOOK_COMMENT,
    INSTAGRAM_COMMENT,
    INSTAGRAM_DM,
    MESSENGER,
    WHATSAPP,
)
from app.channels.facebook_comments import FacebookCommentAdapter
from app.channels.instagram import InstagramDMAdapter
from app.channels.instagram_comments import InstagramCommentAdapter
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

#: Channels this repository can actually send on today.
#:
#: Keeping this list true is the whole job. A channel that ships an adapter
#: without being added here is asserted to refuse while production routes to
#: it, and the fixture below is good enough at pinning the registry to keep
#: that contradiction green.
#:
#: Every id in ALL_CHANNELS is now on the list, so the refusal branch in the
#: parametrised test below is unreachable today. It stays for the next channel
#: added to constants.py and wired nowhere -- which is the regression the
#: parametrisation exists to catch.
IMPLEMENTED = (WHATSAPP, MESSENGER, INSTAGRAM_DM, FACEBOOK_COMMENT, INSTAGRAM_COMMENT)

#: Shaped like Meta's own: a Page comment id is "<post-or-page>_<comment>".
#: Only the shape matters here -- these never reach the network.
COMMENT_ID = "100000000000001_200000000000002"
COMMENT_REPLY_ID = "100000000000001_400000000000004"

#: An Instagram comment id is a plain numeric string with no underscore pair,
#: which is one of the several ways the two comment surfaces are not each
#: other with different ids.
IG_COMMENT_ID = "17900000000000001"
IG_COMMENT_REPLY_ID = "17900000000000002"


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

    That collision stopped being hypothetical once facebook_comment grew a
    real adapter: ``register_adapter`` is last-write-wins, so without this pin
    the result of this file depends on collection order between
    test_channels.py and test_facebook_comments.py.

    All five shipped adapters are listed, and a sixth arriving without being
    added here would make this file assert its channel refuses. The source
    guard at the end of the file is what stops the opposite mistake, where a
    channel is pinned here and forgotten in ``_register_adapters``.
    """
    monkeypatch.setattr(
        registry,
        "_ADAPTERS",
        {
            WHATSAPP: WhatsAppAdapter,
            MESSENGER: MessengerAdapter,
            INSTAGRAM_DM: InstagramDMAdapter,
            FACEBOOK_COMMENT: FacebookCommentAdapter,
            INSTAGRAM_COMMENT: InstagramCommentAdapter,
        },
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


async def test_instagram_dm_routes_to_its_own_adapter(
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
) -> None:
    """Shipped in step 1, and asserted to be missing until now.

    The adapter has been registered in ``_register_adapters`` since it landed,
    so an operator reply to an Instagram customer has been routing correctly
    while the suite claimed the channel refuses. Nothing failed, because the
    fixture pins the registry: the contradiction lived entirely inside the
    tests.
    """
    adapter = outbound_adapter(
        INSTAGRAM_DM,
        whatsapp_client=whatsapp_client,
        settings=_everything_on(),
    )
    try:
        assert isinstance(adapter, InstagramDMAdapter)
        assert adapter.channel == INSTAGRAM_DM
    finally:
        await adapter.aclose()


async def test_facebook_comments_route_to_the_comment_adapter(
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
) -> None:
    """A public channel resolves through the same one function as a private one.

    Worth asserting because it is a claim about what did *not* have to change:
    ``outbound_adapter`` grew no comment branch. The switch check, the
    credential check and the settings-based construction were already
    channel-agnostic, so registering the adapter was the entire wiring.
    """
    settings = _everything_on()
    adapter = outbound_adapter(
        FACEBOOK_COMMENT,
        whatsapp_client=whatsapp_client,
        settings=settings,
    )
    try:
        assert isinstance(adapter, FacebookCommentAdapter)
        assert isinstance(adapter, CommentChannelAdapter)
        assert adapter.channel == FACEBOOK_COMMENT
        assert adapter._settings is settings
        assert adapter._client is not whatsapp_client._client
    finally:
        await adapter.aclose()


async def test_instagram_comments_route_to_the_comment_adapter(
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
) -> None:
    """The fifth channel, and the second public one, through the same function.

    Instagram comments are credentialled by the Instagram pair rather than the
    page pair, so this also pins that ``missing_credentials`` is satisfied by
    ``instagram_account_id`` plus the token fallback: ``_everything_on`` sets
    no ``instagram_access_token``, and the channel is still resolvable because
    ``instagram_token()`` falls back to the page token.
    """
    settings = _everything_on()
    adapter = outbound_adapter(
        INSTAGRAM_COMMENT,
        whatsapp_client=whatsapp_client,
        settings=settings,
    )
    try:
        assert isinstance(adapter, InstagramCommentAdapter)
        assert isinstance(adapter, CommentChannelAdapter)
        assert adapter.channel == INSTAGRAM_COMMENT
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


def test_a_known_channel_with_no_adapter_is_refused(
    whatsapp_client: WhatsAppClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap has to be made on purpose now that every channel is wired.

    This used to point at instagram_comment, which had an id, a profile, a
    switch, credentials and no adapter. It has one as of this step, so the
    assertion is kept by pinning a registry with that channel deliberately
    left out rather than by deleting the test: ``outbound_adapter`` still has
    a branch for a known channel nothing can send on, and it stays covered
    for the next channel added to constants.py and wired nowhere. Falling
    back to another channel there would answer a commenter somewhere they
    never wrote.
    """
    monkeypatch.setattr(
        registry,
        "_ADAPTERS",
        {
            WHATSAPP: WhatsAppAdapter,
            MESSENGER: MessengerAdapter,
            INSTAGRAM_DM: InstagramDMAdapter,
            FACEBOOK_COMMENT: FacebookCommentAdapter,
        },
    )
    with pytest.raises(ChannelUnavailableError) as refusal:
        outbound_adapter(
            INSTAGRAM_COMMENT,
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


async def test_a_comment_reply_is_addressed_to_the_comment_not_its_author(
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one thing a public channel does differently, through the real path.

    Every private channel addresses a person. A public reply belongs
    underneath the question, so the comment id is what travels as the
    recipient and the Graph path is that comment's ``/comments`` edge.
    Addressing the author instead would post the answer as a new top-level
    comment on the page, detached from what it answers -- a mistake that
    reads as working code and looks wrong only on the page itself.

    Routed through ``outbound_adapter`` rather than by constructing the
    adapter directly, so the registration from the previous commit is part of
    what is under test.
    """
    seen: list[tuple[str, dict[str, Any], str]] = []

    async def fake_post(
        path: str, payload: dict[str, Any], *, operation: str
    ) -> dict[str, Any]:
        seen.append((path, payload, operation))
        return {"id": COMMENT_REPLY_ID}

    adapter = outbound_adapter(
        FACEBOOK_COMMENT,
        whatsapp_client=whatsapp_client,
        settings=_everything_on(),
    )
    try:
        monkeypatch.setattr(adapter, "_post", fake_post)
        response = await adapter.send_text(COMMENT_ID, "Thanks for reaching out")
    finally:
        await adapter.aclose()

    path, payload, operation = seen[0]
    assert path == "/" + COMMENT_ID + "/comments"
    assert payload == {"message": "Thanks for reaching out"}
    assert operation == "public_reply"
    # The reply's own id, which is a different comment from the one answered.
    assert response["id"] == COMMENT_REPLY_ID


async def test_an_instagram_comment_reply_travels_as_a_query_parameter(
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same journey on the other public surface, in the other shape.

    Two comment channels do not mean one comment mechanism. Meta documents
    ``message`` as a query string parameter on the Instagram ``/replies``
    edge, while the Page ``/comments`` edge above takes a JSON body -- so the
    two public replies genuinely differ, and sending Instagram's as a body
    would be a 400 per answered comment rather than anything visible in code
    review.

    Routed through ``outbound_adapter`` so the registration this commit's
    sibling added is part of what is exercised, not just the adapter class.
    """
    seen: list[tuple[str, str, dict[str, str] | None, dict[str, Any] | None]] = []

    async def fake_post(
        path: str,
        *,
        operation: str,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        seen.append((path, operation, params, payload))
        return {"id": IG_COMMENT_REPLY_ID}

    adapter = outbound_adapter(
        INSTAGRAM_COMMENT,
        whatsapp_client=whatsapp_client,
        settings=_everything_on(),
    )
    try:
        monkeypatch.setattr(adapter, "_post", fake_post)
        response = await adapter.send_text(IG_COMMENT_ID, "Thanks for reaching out")
    finally:
        await adapter.aclose()

    path, operation, params, payload = seen[0]
    assert path == "/" + IG_COMMENT_ID + "/replies"
    assert params == {"message": "Thanks for reaching out"}
    assert payload is None
    assert operation == "public_reply"
    assert response["id"] == IG_COMMENT_REPLY_ID


def test_every_shipped_adapter_is_imported_by_the_registration_hook() -> None:
    """The one assertion the fixtures in this file cannot make.

    Every fixture here replaces ``registry._ADAPTERS`` with the classes it
    wants, and this module imports each adapter at the top, so a channel whose
    module is missing from ``_register_adapters`` resolves in all of these
    tests while resolving to nothing in production. That is not hypothetical:
    instagram_dm and facebook_comment each shipped a commit ahead of the tests
    that claimed they refused, and nothing went red either time.

    Reading the source is therefore the honest check, the same trick the
    ReplyService guard below uses. The trailing noqa is part of each pattern
    on purpose: "import app.channels.instagram" is a prefix of
    "import app.channels.instagram_comments", so a substring test without it
    would pass for a module nobody imports.
    """
    source = inspect.getsource(outbound._register_adapters)
    for module in (
        "facebook_comments",
        "instagram",
        "instagram_comments",
        "messenger",
        "whatsapp",
    ):
        assert "import app.channels." + module + "  # noqa: F401" in source


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
