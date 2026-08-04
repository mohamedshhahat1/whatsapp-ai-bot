"""Firebase Cloud Messaging HTTP v1 client.

This is the only module in the codebase that knows Firebase exists, in the
same way ``app/integrations/whatsapp.py`` is the only one that knows about the
Meta Graph API. Everything above it speaks in terms of "deliver this title and
body to this token".

HTTP v1 rather than the older ``fcm.googleapis.com/fcm/send`` server-key API,
which Google shut down in June 2024. The cost of that is OAuth: each request
carries a bearer token minted from the service-account key, cached here until
shortly before it expires.

The ``google.auth`` imports are deliberately inside the method that needs
them. Importing this module must stay free for a deployment that has push
turned off, including in tests, where nothing should require a Google
dependency to be installed to collect a test file.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from app.core.logging import get_logger
from app.core.push_config import PushSettings, get_push_settings
from app.core.retry import http_retry

logger = get_logger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_ENDPOINT = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"

# Refresh the access token a minute before it actually expires. A token that
# passes the check here and expires in flight produces a 401, which is not
# retried -- correctly, since a genuine 401 means the credentials are wrong.
_TOKEN_SKEW_SECONDS = 60

# FCM error statuses that mean this token will never work again. Anything else
# is treated as a transient failure and retried.
#
# UNREGISTERED     -- the app was uninstalled, or the token was rotated.
# INVALID_ARGUMENT -- malformed token (also returned for a malformed message,
#                     which is why a rejection is logged, not silent).
# SENDER_ID_MISMATCH -- the token belongs to a different Firebase project.
_DEAD_TOKEN_STATUSES = frozenset(
    {"UNREGISTERED", "INVALID_ARGUMENT", "SENDER_ID_MISMATCH"}
)


class PushNotConfigured(RuntimeError):
    """Raised when a send is attempted without usable credentials."""


class InvalidRegistrationToken(RuntimeError):
    """Raised when FCM says a token is permanently undeliverable.

    A distinct exception rather than a return value because the caller has to
    do something about it -- retire the row -- and an ignored return value
    would leave dead tokens in the table forever, which is exactly the leak
    this feature is supposed to avoid.
    """

    def __init__(self, status: str) -> None:
        super().__init__(f"FCM rejected the registration token: {status}")
        self.status = status


class FcmClient:
    """Sends one message to one device token.

    Knows nothing about conversations, operators or what is worth notifying
    about. Fan-out, privacy and token retirement belong to
    ``app.services.notification_service``.
    """

    def __init__(self, settings: PushSettings | None = None) -> None:
        self._settings = settings or get_push_settings()
        self._client: httpx.AsyncClient | None = None
        self._access_token = ""
        self._expires_at = 0.0
        # Guards the token refresh. Without it, ten notifications arriving
        # together on a cold process would each mint their own access token.
        self._lock = asyncio.Lock()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._settings.push_timeout_seconds
            )
        return self._client

    def _mint_token(self) -> tuple[str, float]:
        """Exchange the service-account key for an access token (blocking).

        Runs in a worker thread via :meth:`_bearer`. google-auth's transport is
        synchronous ``requests``, and calling it directly on the event loop
        would stall every other request in the process for the duration of a
        round trip to Google.
        """
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        try:
            info = json.loads(self._settings.fcm_credentials)
        except ValueError as exc:
            raise PushNotConfigured(
                f"FCM_CREDENTIALS is not valid JSON: {exc}"
            ) from exc
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=[FCM_SCOPE]
        )
        credentials.refresh(Request())
        expiry = credentials.expiry
        # google-auth returns a naive UTC datetime; fall back to the documented
        # one-hour lifetime if it is missing rather than treating the token as
        # already expired and reminting on every send.
        lifetime = expiry.timestamp() if expiry is not None else time.time() + 3600
        return str(credentials.token), lifetime

    async def _bearer(self) -> str:
        """A valid OAuth access token, minted or reused."""
        if not self._settings.configured:
            raise PushNotConfigured(
                "Push is enabled but FCM_PROJECT_ID or FCM_CREDENTIALS is missing"
            )
        async with self._lock:
            if self._access_token and time.time() < self._expires_at:
                return self._access_token
            token, expiry = await asyncio.to_thread(self._mint_token)
            self._access_token = token
            self._expires_at = expiry - _TOKEN_SKEW_SECONDS
            return self._access_token

    def _message(
        self, *, token: str, title: str, body: str, data: dict[str, str]
    ) -> dict[str, Any]:
        """Build the HTTP v1 message envelope.

        ``data`` values must be strings -- FCM rejects the message outright if
        any value is a number -- and the caller is expected to have stringified
        the conversation id already.

        High priority on both platforms because these notifications exist to
        pull a person's attention to a waiting customer; delivered twenty
        minutes later during a doze window, they are worse than useless.
        """
        return {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": data,
                "android": {
                    "priority": "high",
                    "notification": {
                        "channel_id": self._settings.push_android_channel,
                        "default_sound": True,
                    },
                },
                "apns": {
                    "headers": {"apns-priority": "10"},
                    "payload": {"aps": {"sound": "default", "badge": 1}},
                },
            }
        }

    @staticmethod
    def _status_of(response: httpx.Response) -> str:
        """Pull FCM's machine-readable error status out of a response.

        Best effort: an error body from a proxy rather than from FCM will not
        have this shape, and an empty string then falls through to the
        transient path -- the safe direction, since it retries rather than
        retiring a token that may be fine.
        """
        try:
            payload = response.json()
        except ValueError:
            return ""
        error = payload.get("error") or {}
        for detail in error.get("details") or []:
            status = detail.get("errorCode")
            if status:
                return str(status)
        return str(error.get("status") or "")

    @http_retry()
    async def send(
        self, *, token: str, title: str, body: str, data: dict[str, str]
    ) -> str:
        """Hand one notification to Firebase; return the accepted message name.

        Raises :class:`InvalidRegistrationToken` for a permanently dead token
        and lets transient failures propagate as ``httpx`` errors, which the
        shared retry policy retries with exponential backoff and jitter.

        The return value is FCM's message *name*, and it means "accepted for
        delivery" -- not "shown on a phone". FCM issues no delivery receipt,
        so nothing here can honestly claim more than that.
        """
        bearer = await self._bearer()
        url = FCM_ENDPOINT.format(project=self._settings.fcm_project_id)
        response = await self._http().post(
            url,
            json=self._message(token=token, title=title, body=body, data=data),
            headers={"Authorization": f"Bearer {bearer}"},
        )
        if response.status_code in (400, 403, 404):
            status = self._status_of(response)
            if status in _DEAD_TOKEN_STATUSES:
                raise InvalidRegistrationToken(status)
            # A 401/403 for the CREDENTIALS, or a malformed envelope: a bug or
            # a misconfiguration, and retrying it would only hide it.
            logger.error(
                "fcm_request_rejected",
                status_code=response.status_code,
                fcm_status=status or "unknown",
            )
        response.raise_for_status()
        return str(response.json().get("name", ""))

    async def aclose(self) -> None:
        """Release the connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
