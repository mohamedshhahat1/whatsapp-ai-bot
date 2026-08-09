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

These tests cover resolution only -- which channels an object can deliver.
Whether those channels are switched on, configured, or have an adapter is a
separate question, asked in tests/test_channels.py and
tests/test_outbound_routing.py.
"""

from app.channels import registry
from app.channels.constants import (
    FACEBOOK_COMMENT,
    INSTAGRAM_COMMENT,
    INSTAGRAM_DM,
    MESSENGER,
)


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
