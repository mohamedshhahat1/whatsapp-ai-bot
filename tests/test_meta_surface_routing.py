"""One Meta delivery can carry two surfaces, and both have to be found.

Meta subscribes a single URL for the whole app and names the surface in the
envelope's ``object`` field. That field is not specific enough to identify a
channel: ``page`` means Messenger when an entry carries ``messaging`` and
Facebook comments when it carries ``changes``, and one delivery may carry
both. ``instagram`` splits the same way between Instagram DM and Instagram
comments.

Routing such a delivery to a single channel would file half of it under the
wrong surface. That is not a cosmetic error: ``conversations.channel`` is what
every per-channel analytics figure is grouped by, and comment-to-DM conversion
is measured precisely by telling a comment apart from the DM it produced.

Two levels are covered here: which channels an object can deliver, and which
adapters this deployment can actually build for them. Whether a delivery is
then dispatched to each of them is asserted against the real route and the
real Celery task, in tests/test_meta_webhook_instagram.py and
tests/test_meta_task_routing.py.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from app.channels import registry
from app.channels.config import ChannelSettings
from app.channels.constants import (
    FACEBOOK_COMMENT,
    INSTAGRAM_COMMENT,
    INSTAGRAM_DM,
    MESSENGER,
)
from app.channels.facebook_comments import FacebookCommentAdapter
from app.channels.instagram import InstagramDMAdapter
from app.channels.messenger import MessengerAdapter
from app.channels.outbound import meta_inbound_adapters


def _settings(**overrides: Any) -> ChannelSettings:
    """Channel settings with every Meta surface on and credentialed.

    ``_env_file=None`` so a developer's own .env cannot decide what these
    tests assert. The credentials are placeholders; nothing here reaches the
    network. Overrides are merged before splatting rather than passed
    alongside these defaults, because passing both raises TypeError for any
    key that appears twice.
    """
    values: dict[str, Any] = {
        "enable_messenger": True,
        "enable_instagram_dm": True,
        "enable_facebook_comments": True,
        "enable_instagram_comments": True,
        "facebook_page_id": "100000000000001",
        "facebook_page_access_token": "unit-test-placeholder",
        "instagram_account_id": "17841400000000001",
    }
    values.update(overrides)
    return ChannelSettings(_env_file=None, **values)


@pytest.fixture
def real_adapters() -> Iterator[None]:
    """Pin the real adapter classes over anything another module left behind.

    ``register_adapter`` is last-write-wins and never raises on a duplicate,
    and tests/test_channels.py registers a zero-argument ``_FakeAdapter`` for
    facebook_comment without cleaning up after itself. Whenever that module
    runs first, resolution here would build the fake and fail on its
    constructor -- a test-ordering problem wearing the costume of a routing
    bug.

    Restored afterwards, so this fixture cannot become the pollution it exists
    to guard against.
    """
    previous = dict(registry._ADAPTERS)
    registry._ADAPTERS.update(
        {
            MESSENGER: MessengerAdapter,
            INSTAGRAM_DM: InstagramDMAdapter,
            FACEBOOK_COMMENT: FacebookCommentAdapter,
        }
    )
    yield
    registry._ADAPTERS.clear()
    registry._ADAPTERS.update(previous)


# --------------------------------------------------------------------------
# Which channels an object can deliver
# --------------------------------------------------------------------------


def test_a_page_delivery_is_messenger_or_a_facebook_comment() -> None:
    assert registry.meta_dm_channel("page") == MESSENGER
    assert registry.meta_comment_channel("page") == FACEBOOK_COMMENT


def test_an_instagram_delivery_is_a_dm_or_an_instagram_comment() -> None:
    assert registry.meta_dm_channel("instagram") == INSTAGRAM_DM
    assert registry.meta_comment_channel("instagram") == INSTAGRAM_COMMENT


def test_both_surfaces_of_one_object_are_returned_together() -> None:
    assert registry.meta_channels_for_object("page") == (
        MESSENGER,
        FACEBOOK_COMMENT,
    )
    assert registry.meta_channels_for_object("instagram") == (
        INSTAGRAM_DM,
        INSTAGRAM_COMMENT,
    )


def test_the_private_surface_comes_first() -> None:
    """Dispatch follows this order, so it is fixed rather than incidental."""
    for object_type in ("page", "instagram"):
        first, second = registry.meta_channels_for_object(object_type)
        assert first in registry.META_DM_CHANNELS.values()
        assert second in registry.META_COMMENT_CHANNELS.values()


def test_an_object_this_app_does_not_serve_resolves_to_nothing() -> None:
    """Meta adds products to an existing subscription without asking."""
    for object_type in ("whatsapp_business_account", "permissions", "user", ""):
        assert registry.meta_dm_channel(object_type) is None
        assert registry.meta_comment_channel(object_type) is None
        assert registry.meta_channels_for_object(object_type) == ()


def test_every_meta_channel_is_reachable_from_some_object() -> None:
    """A new Meta channel with no resolver would never receive a delivery.

    It would pass every other test -- registered, enabled, credentialed, with
    an adapter -- and still never be called, because nothing maps an envelope
    to it. This is the assertion that fails instead.
    """
    objects = set(registry.META_DM_CHANNELS) | set(registry.META_COMMENT_CHANNELS)
    reachable: set[str] = set()
    for object_type in objects:
        reachable.update(registry.meta_channels_for_object(object_type))
    assert reachable == set(registry.META_CHANNELS)


def test_a_channel_is_never_both_a_dm_and_a_comment_surface() -> None:
    """The two mappings share their keys. Sharing a value as well would make
    attribution depend on which lookup happened to run first."""
    dm = set(registry.META_DM_CHANNELS.values())
    comments = set(registry.META_COMMENT_CHANNELS.values())
    assert dm.isdisjoint(comments)


def test_both_mappings_describe_the_same_objects() -> None:
    """Every Meta object with a DM surface also has a comment surface.

    True for both objects today. Should a future object have only one, this is
    where that gets stated deliberately, rather than being discovered as a
    hole in dispatch.
    """
    assert set(registry.META_DM_CHANNELS) == set(registry.META_COMMENT_CHANNELS)


# --------------------------------------------------------------------------
# Which adapters this deployment can build for them
# --------------------------------------------------------------------------


async def test_a_page_delivery_resolves_both_of_its_surfaces(
    real_adapters: None,
) -> None:
    adapters = meta_inbound_adapters("page", settings=_settings())
    try:
        assert [a.channel for a in adapters] == [MESSENGER, FACEBOOK_COMMENT]
        assert isinstance(adapters[0], MessengerAdapter)
        assert isinstance(adapters[1], FacebookCommentAdapter)
    finally:
        for adapter in adapters:
            await adapter.aclose()


async def test_switching_comments_off_leaves_the_dm_surface_alone(
    real_adapters: None,
) -> None:
    """Exactly what every existing Messenger deployment already does."""
    adapters = meta_inbound_adapters(
        "page",
        settings=_settings(enable_facebook_comments=False),
    )
    try:
        assert [a.channel for a in adapters] == [MESSENGER]
    finally:
        for adapter in adapters:
            await adapter.aclose()


async def test_comments_can_be_served_with_messenger_switched_off(
    real_adapters: None,
) -> None:
    """A page that answers comments but not DMs is a legitimate deployment.

    Before per-surface resolution this combination could not work at all: one
    channel was resolved per object, and that channel was off.
    """
    adapters = meta_inbound_adapters(
        "page",
        settings=_settings(enable_messenger=False),
    )
    try:
        assert [a.channel for a in adapters] == [FACEBOOK_COMMENT]
    finally:
        for adapter in adapters:
            await adapter.aclose()


async def test_instagram_resolves_only_its_dm_surface_for_now(
    real_adapters: None,
) -> None:
    """Instagram comments have no adapter yet, so only the DM surface builds.

    Asserted rather than left implicit: the channel is enabled and credentialed
    here, so a single-element result is evidence that a channel without an
    adapter is skipped rather than crashing dispatch or resolving to None.
    Step 3 writes that adapter and this becomes two.
    """
    adapters = meta_inbound_adapters("instagram", settings=_settings())
    try:
        assert [a.channel for a in adapters] == [INSTAGRAM_DM]
    finally:
        for adapter in adapters:
            await adapter.aclose()


def test_an_unserved_object_builds_no_adapters_at_all() -> None:
    """No settings and no fixture: nothing should be constructed to find out."""
    assert meta_inbound_adapters("whatsapp_business_account") == []


async def test_every_surface_switched_off_resolves_nothing(
    real_adapters: None,
) -> None:
    """The objects stay subscribed at Meta whatever this deployment thinks."""
    adapters = meta_inbound_adapters(
        "page",
        settings=_settings(
            enable_messenger=False,
            enable_facebook_comments=False,
        ),
    )
    assert adapters == []


async def test_a_surface_missing_its_credentials_is_skipped_not_raised(
    real_adapters: None,
) -> None:
    """Enabled without a page token is a misconfiguration, not an exception.

    Both page surfaces need the same two credentials, so blanking the token
    removes both of them and the delivery resolves to nothing. The webhook
    still has to answer 200, which is why this returns rather than raises.
    """
    adapters = meta_inbound_adapters(
        "page",
        settings=_settings(facebook_page_access_token=""),
    )
    assert adapters == []
