import 'dart:async';
import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../config/app_config.dart';
import '../storage/secure_storage.dart';

/// WebSocket event types from the backend.
///
/// Anything not listed here parses as [unknown] and is dropped by listeners,
/// so a backend event with no member below is invisible to this app no matter
/// how faithfully it is published. That is how the session lifecycle events
/// went unnoticed on both clients.
enum WsEventType {
  ready,
  heartbeat,
  conversationActivity,
  conversationHandoff,

  /// The idle sweep ended a session. Carries status, closed_at and user_id.
  conversationClosed,

  /// A customer came back, or an operator revived a closed session.
  conversationReopened,

  unknown,
}

/// Maps the backend's wire strings onto [WsEventType].
///
/// A top-level function rather than a static on an extension. It was
/// previously declared `static` inside an UNNAMED `extension on WsEventType`,
/// and Dart resolves static extension members through the extension's own
/// name -- which an unnamed extension does not have -- so `WsEventType
/// .fromString(...)` could never compile.
WsEventType wsEventTypeFromString(String s) => switch (s) {
  'ready' => WsEventType.ready,
  'heartbeat' => WsEventType.heartbeat,
  'conversation.activity' => WsEventType.conversationActivity,
  'conversation.handoff' => WsEventType.conversationHandoff,
  'conversation.closed' => WsEventType.conversationClosed,
  'conversation.reopened' => WsEventType.conversationReopened,
  _ => WsEventType.unknown,
};

/// Parsed WebSocket event.
///
/// Every getter is nullable and read defensively: the bus carries several
/// event shapes and a field belonging to one is simply absent from the
/// others.
class WsEvent {
  final WsEventType type;
  final Map<String, dynamic> raw;
  WsEvent(this.type, this.raw);

  int? get conversationId => raw['conversation_id'] as int?;
  int? get userId => raw['user_id'] as int?;
  bool? get inbound => raw['inbound'] as bool?;
  String? get mode => raw['mode'] as String?;
  String? get assignedOperator => raw['assigned_operator'] as String?;
  String? get reason => raw['reason'] as String?;
  String? get tag => raw['tag'] as String?;
  String? get at => raw['at'] as String?;

  /// 'active' or 'closed'. Present on closed and reopened events.
  String? get status => raw['status'] as String?;
  String? get closedAt => raw['closed_at'] as String?;
  String? get updatedAt => raw['updated_at'] as String?;

  /// True for events that change a conversation's lifecycle state, as opposed
  /// to adding a message or moving ownership.
  bool get isLifecycle =>
      type == WsEventType.conversationClosed ||
      type == WsEventType.conversationReopened;
}

enum WsConnectionState {
  disconnected,
  connecting,
  authenticated,
  connected,
  reconnecting,
  failed,
}

class WebSocketService {
  WebSocketService(this._storage);

  final SecureStorage _storage;
  WebSocketChannel? _channel;

  /// dynamic because WebSocketChannel.stream is Stream<dynamic>; _onMessage
  /// checks the payload type itself rather than trusting a narrower argument.
  StreamSubscription<dynamic>? _sub;
  Timer? _heartbeatTimer;
  Timer? _reconnectTimer;
  int _reconnectAttempts = 0;
  bool _manuallyClosed = false;
  String? _baseUrl;

  final _stateController = StreamController<WsConnectionState>.broadcast();
  final _eventController = StreamController<WsEvent>.broadcast();

  /// Stream of connection state changes.
  Stream<WsConnectionState> get stateStream => _stateController.stream;
  WsConnectionState _state = WsConnectionState.disconnected;
  WsConnectionState get state => _state;

  /// Stream of parsed events.
  Stream<WsEvent> get eventStream => _eventController.stream;

  /// Connect to the backend WebSocket.
  Future<void> connect(String baseUrl) async {
    _baseUrl = baseUrl;
    _manuallyClosed = false;
    await _doConnect();
  }

  Future<void> _doConnect() async {
    if (_baseUrl == null) return;
    _setState(WsConnectionState.connecting);

    final apiKey = await _storage.getApiKey();
    if (apiKey == null || apiKey.isEmpty) {
      _setState(WsConnectionState.failed);
      return;
    }

    final wsUrl = wsUrlFromHttp(_baseUrl!);
    try {
      _channel = WebSocketChannel.connect(Uri.parse(wsUrl));
      _sub = _channel!.stream.listen(
        (data) => _onMessage(data),
        onDone: () => _onDone(),
        onError: (e) => _onDone(),
      );

      // Send auth as first frame (backend requires this).
      _channel!.sink.add(jsonEncode({'api_key': apiKey}));
    } catch (e) {
      _scheduleReconnect();
    }
  }

  void _onMessage(dynamic data) {
    if (data is! String) return;
    Map<String, dynamic> json;
    try {
      json = jsonDecode(data) as Map<String, dynamic>;
    } catch (_) {
      return;
    }

    final typeStr = json['type'] as String? ?? '';
    final type = wsEventTypeFromString(typeStr);
    final event = WsEvent(type, json);

    switch (type) {
      case WsEventType.ready:
        _setState(WsConnectionState.authenticated);
        _setState(WsConnectionState.connected);
        _reconnectAttempts = 0;
        _startHeartbeat();
        break;
      case WsEventType.heartbeat:
        // Backend heartbeat — connection is alive.
        _startHeartbeat();
        break;
      default:
        // Everything else, including unknown types, reaches listeners. A
        // future backend event should make this app refresh rather than
        // ignore it.
        _startHeartbeat();
        _eventController.add(event);
    }
  }

  void _onDone() {
    _stopHeartbeat();
    if (_manuallyClosed) {
      _setState(WsConnectionState.disconnected);
      return;
    }
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    _setState(WsConnectionState.reconnecting);
    _reconnectAttempts++;
    // Exponential backoff, doubling per attempt and capped both ways. The
    // shift is clamped to 5 so the multiplier cannot overflow after a long
    // outage.
    final backoffSeconds = AppConfig.wsInitialReconnectDelay.inSeconds *
        (1 << (_reconnectAttempts - 1).clamp(0, 5));
    final delay = Duration(
      seconds: backoffSeconds.clamp(1, AppConfig.wsMaxReconnectDelay.inSeconds),
    );
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(delay, () => _doConnect());
  }

  void _startHeartbeat() {
    _heartbeatTimer?.cancel();
    // The backend sends a heartbeat every 20s. Any frame at all resets this
    // watchdog, so 30s of total silence means the connection is stale even
    // though the socket has not reported an error -- which is the usual way a
    // mobile connection dies.
    _heartbeatTimer = Timer(const Duration(seconds: 30), () {
      _onDone();
    });
  }

  void _stopHeartbeat() {
    _heartbeatTimer?.cancel();
    _heartbeatTimer = null;
  }

  void _setState(WsConnectionState s) {
    _state = s;
    _stateController.add(s);
  }

  /// Manually disconnect.
  Future<void> disconnect() async {
    _manuallyClosed = true;
    _reconnectTimer?.cancel();
    _stopHeartbeat();
    await _sub?.cancel();
    await _channel?.sink.close();
    _setState(WsConnectionState.disconnected);
  }

  void dispose() {
    disconnect();
    _stateController.close();
    _eventController.close();
  }
}

final webSocketServiceProvider = Provider<WebSocketService>((ref) {
  final storage = ref.watch(secureStorageProvider);
  return WebSocketService(storage);
});
