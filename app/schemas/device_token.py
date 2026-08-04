"""Request and response bodies for mobile device registration."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Spelled as literals rather than referencing the model constants because
# typing.Literal only accepts literal values. tests/test_push_notifications.py
# asserts the two lists agree, so a platform added to the model without being
# added here fails in CI rather than at the API boundary.
Platform = Literal["android", "ios"]
PrivacyMode = Literal["private", "preview"]


class DeviceTokenRegister(BaseModel):
    """POST body: this device would like to receive notifications.

    The length floor is a sanity check, not security -- a real FCM token is
    around 160 characters, and an empty string would otherwise be stored and
    then fail on every send forever.
    """

    token: str = Field(min_length=32, max_length=512)
    platform: Platform
    # Optional so an older build of the app keeps working; omitting it means
    # private, which is the safe default rather than the permissive one.
    notification_privacy: PrivacyMode = "private"


class DeviceTokenDelete(BaseModel):
    """DELETE body: stop sending to this device.

    The token travels in the body rather than the URL on purpose. A push token
    is a bearer-ish identifier for a phone, and query strings end up in access
    logs, proxy logs and browser history in a way request bodies do not.
    """

    token: str = Field(min_length=32, max_length=512)


class DeviceTokenRead(BaseModel):
    """What registration returns.

    Deliberately does not echo the token back. The caller already has it, and
    a response body is one more place it can be logged.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: str
    notification_privacy: str
    enabled: bool
    last_seen_at: datetime
