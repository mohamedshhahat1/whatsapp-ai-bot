import 'dart:async';
import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../config/app_config.dart';
import '../storage/secure_storage.dart';

/// WebSocket event types from the backend.
enum WsEventType {
  ready,
  heartbeat,
  conversationActivity,
  conversationHandoff,
  unknown,
}

extension on WsEventType {
  static WsEventType fromString(String s) => switch (s) {
    'ready' => WsEventType.ready,
    'heartbeat' => WsEventType.heartbeat,
    'conversation.activity' => WsEventType.conversationActivity,
    'conversation.handoff' => WsEventType.conversationHandoff,
    _ => WsEventType.unknown,
  };
}

/// Parsed WebSocket event.
class WsEvent {
  final WsEventType type;
  final Map<String, dynamic> raw;
  WsEvent(this.type, this.raw);

  int? get conversationId => raw['conversation_id'] as int?;
  bool? get inbound => raw['inbound'] as bool?;
  String? get mode => raw['mode'] as String?;
  String? get assignedOperator => raw['assigned_operator'] as String?;
  String? get reason => raw['reason'] as String?;
  String? get tag => raw['tag'] as String?;
  String? get at => raw['at'] as String?;
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
  StreamSubscription? _sub;
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
    final type = WsEventType.fromString(typeStr);
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
        break;
      default:
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
    final delay = Duration(seconds:
      (AppConfig.wsInitialReconnectDelay.inSeconds *
          (1 << (_reconnectAttempts - 1).clamp(0, 5))).
      .clamp(1, AppConfig.wsMaxReconnectDelay.inSeconds),
    );
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(delay, () => _doConnect());
  }

  void _startHeartbeat() {
    _heartbeatTimer?.cancel();
    // Backend sends heartbeats every 20s. We just need to track that we're alive.
    _heartbeatTimer = Timer(const Duration(seconds: 30), () {
      // If no message in 30s, consider connection stale.
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
