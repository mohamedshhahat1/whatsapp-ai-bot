import 'dart:async';

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

  /// Set once the backend has refused an action with 409
  /// conversation_superseded, meaning this customer has already started a
  /// newer session and this one can never be written to again.
  ///
  /// Sticky on purpose: unlike an ordinary error there is no useful retry, so
  /// the controls stay disabled rather than inviting the operator to try
  /// again.
  final bool superseded;

  /// The customer behind this session and their earlier ones. Null until the
  /// history sheet is opened, since most conversations are never inspected
  /// that way.
  final CustomerHistory? history;

  const ChatDetailState({
    this.detail,
    this.status = ChatDetailStatus.idle,
    this.errorMessage,
    this.isTyping = false,
    this.superseded = false,
    this.history,
  });

  /// A closed session is still writable: replying or taking over reopens it.
  /// A SUPERSEDED one is not, which is the distinction the UI needs.
  bool get isClosed => detail?.status == statusClosed;
  bool get canAct => !superseded;

  ChatDetailState copyWith({
    ConversationDetail? detail,
    ChatDetailStatus? status,
    String? errorMessage,
    bool? isTyping,
    bool? superseded,
    CustomerHistory? history,
    /// Explicit, because `errorMessage ?? this.errorMessage` cannot express
    /// "clear it" -- the previous error survived every subsequent success and
    /// stayed on screen indefinitely.
    bool clearError = false,
  }) => ChatDetailState(
    detail: detail ?? this.detail,
    status: status ?? this.status,
    errorMessage: clearError ? null : (errorMessage ?? this.errorMessage),
    isTyping: isTyping ?? this.isTyping,
    superseded: superseded ?? this.superseded,
    history: history ?? this.history,
  );
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
    state = state.copyWith(status: ChatDetailStatus.loading, clearError: true);
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
        // Without these an open transcript kept showing a live session after
        // the idle sweep had closed it, and the reply box kept promising a
        // delivery it could no longer make.
        case WsEventType.conversationClosed: _refreshSilent(); break;
        case WsEventType.conversationReopened: _refreshSilent(); break;
        default: break;
      }
    });
  }

  Future<void> _refreshSilent() async {
    if (_conversationId == null) return;
    try { final detail = await _repo.getConversation(_conversationId!); state = state.copyWith(detail: detail); } catch (_) {}
  }

  /// Loads the customer's other sessions. Operator-facing only -- none of it
  /// is sent to the model, which still sees the current session alone.
  Future<void> loadHistory() async {
    if (_conversationId == null) return;
    try {
      final history = await _repo.getHistory(_conversationId!);
      state = state.copyWith(history: history);
    } catch (_) {
      // A missing history panel is not worth breaking the transcript over.
    }
  }

  /// Sends a manual reply. If the session has closed the backend reopens it
  /// first, so the reply and the customer's answer stay in one conversation.
  Future<bool> sendReply(String text) async {
    if (_conversationId == null) return false;
    state = state.copyWith(status: ChatDetailStatus.sending, clearError: true);
    try {
      await _repo.sendReply(_conversationId!, text);
      await _refreshSilent();
      state = state.copyWith(status: ChatDetailStatus.idle);
      return true;
    } on ConversationSuperseded catch (e) {
      state = state.copyWith(
        status: ChatDetailStatus.error, errorMessage: e.message, superseded: true,
      );
      return false;
    } on Failure catch (e) { state = state.copyWith(status: ChatDetailStatus.error, errorMessage: e.message); return false; }
  }

  Future<bool> takeOver() async {
    if (_conversationId == null) return false;
    state = state.copyWith(clearError: true);
    try { final operator = await _storage.getOperatorName(); await _repo.takeOver(_conversationId!, operator: operator); await _refreshSilent(); return true; }
    on ConversationSuperseded catch (e) { state = state.copyWith(errorMessage: e.message, superseded: true); return false; }
    on Failure catch (e) { state = state.copyWith(errorMessage: e.message); return false; }
  }

  Future<bool> resumeAi() async {
    if (_conversationId == null) return false;
    state = state.copyWith(clearError: true);
    try { await _repo.resumeAi(_conversationId!); await _refreshSilent(); return true; }
    on ConversationSuperseded catch (e) { state = state.copyWith(errorMessage: e.message, superseded: true); return false; }
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
