import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/error/failures.dart';
import '../../core/storage/secure_storage.dart';
import '../../core/websocket/websocket_service.dart';
import 'chat_models.dart';
import 'chat_repository.dart';

enum ChatDetailStatus { idle, loading, refreshing, sending, error }

class ChatDetailState {
  final ConversationDetail? detail;
  final ChatDetailStatus status;
  final String? errorMessage;
  final bool isTyping;
  const ChatDetailState({this.detail, this.status = ChatDetailStatus.idle, this.errorMessage, this.isTyping = false});

  ChatDetailState copyWith({ConversationDetail? detail, ChatDetailStatus? status, String? errorMessage, bool? isTyping}) =>
    ChatDetailState(detail: detail ?? this.detail, status: status ?? this.status, errorMessage: errorMessage ?? this.errorMessage, isTyping: isTyping ?? this.isTyping);
}

class ChatDetailNotifier extends StateNotifier<ChatDetailState> {
  ChatDetailNotifier(this._repo, this._wsService, this._storage) : super(const ChatDetailState());
  final ChatRepository _repo;
  final WebSocketService _wsService;
  final SecureStorage _storage;
  StreamSubscription? _wsSub;
  int? _conversationId;

  Future<void> load(int conversationId) async {
    _conversationId = conversationId;
    state = state.copyWith(status: ChatDetailStatus.loading, errorMessage: null);
    try {
      final detail = await _repo.getConversation(conversationId);
      state = ChatDetailState(detail: detail, status: ChatDetailStatus.idle);
      _subscribeWs();
    } on Failure catch (e) { state = state.copyWith(status: ChatDetailStatus.error, errorMessage: e.message); }
  }

  void _subscribeWs() {
    _wsSub?.cancel();
    _wsSub = _wsService.eventStream.listen((event) {
      if (event.conversationId != _conversationId) return;
      switch (event.type) {
        case WsEventType.conversationActivity: _refreshSilent(); break;
        case WsEventType.conversationHandoff: _refreshSilent(); break;
        default: break;
      }
    });
  }

  Future<void> _refreshSilent() async {
    if (_conversationId == null) return;
    try { final detail = await _repo.getConversation(_conversationId!); state = state.copyWith(detail: detail); } catch (_) {}
  }

  Future<bool> sendReply(String text) async {
    if (_conversationId == null) return false;
    state = state.copyWith(status: ChatDetailStatus.sending);
    try {
      await _repo.sendReply(_conversationId!, text);
      await _refreshSilent();
      state = state.copyWith(status: ChatDetailStatus.idle);
      return true;
    } on Failure catch (e) { state = state.copyWith(status: ChatDetailStatus.error, errorMessage: e.message); return false; }
  }

  Future<bool> takeOver() async {
    if (_conversationId == null) return false;
    try { final operator = await _storage.getOperatorName(); await _repo.takeOver(_conversationId!, operator: operator); await _refreshSilent(); return true; }
    on Failure catch (e) { state = state.copyWith(errorMessage: e.message); return false; }
  }

  Future<bool> resumeAi() async {
    if (_conversationId == null) return false;
    try { await _repo.resumeAi(_conversationId!); await _refreshSilent(); return true; }
    on Failure catch (e) { state = state.copyWith(errorMessage: e.message); return false; }
  }

  Future<bool> deleteConversation() async {
    if (_conversationId == null) return false;
    try { await _repo.deleteConversation(_conversationId!); return true; }
    on Failure catch (e) { state = state.copyWith(errorMessage: e.message); return false; }
  }

  @override
  void dispose() { _wsSub?.cancel(); super.dispose(); }
}

final chatDetailProvider = StateNotifierProvider.family<ChatDetailNotifier, ChatDetailState, int>((ref, conversationId) {
  return ChatDetailNotifier(ref.watch(chatRepositoryProvider), ref.watch(webSocketServiceProvider), ref.watch(secureStorageProvider));
});
