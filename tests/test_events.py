"""The realtime dashboard stream: handshake, rejection, and fan-out.

The fan-out test is the one that matters. It publishes to Redis the way the
Celery worker does and asserts the event arrives on a WebSocket served by the
API -- the cross-process hop that a single-process test would not exercise.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import get_settings
from app.core.events import CHANNEL, conversation_activity

POLICY_VIOLATION = 1008


def redis_available() -> bool:
    try:
        from redis import Redis

        client = Redis.from_url(get_settings().redis_url)
        try:
            client.ping()
        finally:
            client.close()
        return True
    except Exception:
        return False


def test_activity_event_carries_no_customer_data() -> None:
    """The bus gets a pointer, not the conversation.

    If the payload grew to include the message body or the wa_id (a phone
    number), Redis would become an unauthenticated copy of customer data.
    """
    event = conversation_activity(conversation_id=7, inbound=True)
    assert event["type"] == "conversation.activity"
    assert event["conversation_id"] == 7
    assert event["inbound"] is True
    assert set(event) == {"type", "conversation_id", "inbound", "at"}


def test_stream_rejects_a_wrong_key(client: TestClient) -> None:
    with client.websocket_connect("/ws/events") as socket:
        socket.send_json({"api_key": "not-the-admin-key"})
        with pytest.raises(WebSocketDisconnect) as failure:
            socket.receive_json()
    assert failure.value.code == POLICY_VIOLATION


def test_stream_rejects_a_malformed_handshake(client: TestClient) -> None:
    with client.websocket_connect("/ws/events") as socket:
        socket.send_text("not json at all")
        with pytest.raises(WebSocketDisconnect) as failure:
            socket.receive_json()
    assert failure.value.code == POLICY_VIOLATION


def test_stream_rejects_an_empty_key(client: TestClient) -> None:
    with client.websocket_connect("/ws/events") as socket:
        socket.send_json({"api_key": ""})
        with pytest.raises(WebSocketDisconnect) as failure:
            socket.receive_json()
    assert failure.value.code == POLICY_VIOLATION


@pytest.mark.skipif(not redis_available(), reason="No Redis reachable at REDIS_URL")
def test_published_event_reaches_a_subscribed_dashboard(client: TestClient) -> None:
    from redis import Redis

    settings = get_settings()
    with client.websocket_connect("/ws/events") as socket:
        socket.send_json({"api_key": settings.admin_api_key})
        # "ready" is sent only after the subscription exists, so nothing
        # published after this point can be missed.
        assert socket.receive_json()["type"] == "ready"

        payload = json.dumps(
            conversation_activity(conversation_id=99, inbound=True)
        )
        publisher = Redis.from_url(settings.redis_url)
        try:
            delivered = 0
            for _ in range(50):
                delivered = publisher.publish(CHANNEL, payload)
                if delivered:
                    break
                time.sleep(0.1)
            assert delivered, "no subscriber was attached to the channel"
        finally:
            publisher.close()

        event = socket.receive_json()

    assert event["type"] == "conversation.activity"
    assert event["conversation_id"] == 99
    assert event["inbound"] is True
