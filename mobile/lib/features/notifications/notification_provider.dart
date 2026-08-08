import 'dart:async';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/websocket/websocket_service.dart';
import '../chat/chat_models.dart' show tagSalesLead;

class AppNotification {
  final int id;
  final String title;
  final String body;
  final int? conversationId;
  final String? tag;
  final DateTime timestamp;
  bool isRead;

  AppNotification({
    required this.id, required this.title, required this.body,
    this.conversationId, this.tag, required this.timestamp, this.isRead = false,
  });
}

class NotificationNotifier extends StateNotifier<List<AppNotification>> {
  NotificationNotifier(this._wsService) : super([]) { _sub = _wsService.eventStream.listen(_onEvent); }
  final WebSocketService _wsService;
  StreamSubscription? _sub;
  int _nextId = 0;

  void _onEvent(WsEvent event) {
    switch (event.type) {
      case WsEventType.conversationActivity:
        if (event.inbound == true && event.conversationId != null) {
          state = [AppNotification(id: _nextId++, title: 'New message', body: 'New customer message in conversation #${event.conversationId}', conversationId: event.conversationId, timestamp: DateTime.now()), ...state];
        }
        break;
      case WsEventType.conversationHandoff:
        state = [AppNotification(id: _nextId++, title: event.tag == tagSalesLead ? '🔥 Sales Lead!' : 'Handoff', body: 'Conversation #${event.conversationId} → ${event.mode} mode${event.assignedOperator != null ? ' (${event.assignedOperator})' : ''}', conversationId: event.conversationId, tag: event.tag, timestamp: DateTime.now()), ...state];
        break;
      default: break;
    }
  }

  void markRead(int id) { state = state.map((n) => n.id == id ? (n..isRead = true) : n).toList(); }
  void markAllRead() { state = state.map((n) => n..isRead = true).toList(); }
  // Every state transition belongs here rather than in a widget. The state
  // setter is protected, and writing it from the outside also meant reading
  // the list a second time to compute the replacement.
  void remove(int id) { state = state.where((n) => n.id != id).toList(); }
  void clear() { state = []; }
  int get unreadCount => state.where((n) => !n.isRead).length;

  @override
  void dispose() { _sub?.cancel(); super.dispose(); }
}

final notificationProvider = StateNotifierProvider<NotificationNotifier, List<AppNotification>>((ref) {
  return NotificationNotifier(ref.watch(webSocketServiceProvider));
});

final unreadCountProvider = Provider<int>((ref) {
  return ref.watch(notificationProvider).where((n) => !n.isRead).length;
});
