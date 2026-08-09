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
