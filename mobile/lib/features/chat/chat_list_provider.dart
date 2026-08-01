import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/config/app_config.dart';
import '../../core/error/failures.dart';
import '../../core/storage/secure_storage.dart';
import '../../core/websocket/websocket_service.dart';
import 'chat_models.dart';
import 'chat_repository.dart';

enum ChatListStatus { idle, loading, refreshing, loadingMore, error }

class ChatListState {
  final List<Conversation> conversations;
  final ChatListStatus status;
  final String? errorMessage;
  final bool hasMore;
  final int offset;
  final String? searchQuery;
  final String? modeFilter;
  final Set<int> unreadIds;

  const ChatListState({
    this.conversations = const [], this.status = ChatListStatus.idle,
    this.errorMessage, this.hasMore = true, this.offset = 0,
    this.searchQuery, this.modeFilter, this.unreadIds = const {},
  });

  ChatListState copyWith({
    List<Conversation>? conversations, ChatListStatus? status, String? errorMessage,
    bool? hasMore, int? offset, String? searchQuery, String? modeFilter, Set<int>? unreadIds,
  }) => ChatListState(
    conversations: conversations ?? this.conversations, status: status ?? this.status,
    errorMessage: errorMessage, hasMore: hasMore ?? this.hasMore, offset: offset ?? this.offset,
    searchQuery: searchQuery ?? this.searchQuery, modeFilter: modeFilter ?? this.modeFilter,
    unreadIds: unreadIds ?? this.unreadIds,
  );
}

class ChatListNotifier extends StateNotifier<ChatListState> {
  ChatListNotifier(this._repo, this._wsService, this._storage) : super(const ChatListState()) { _init(); }

  final ChatRepository _repo;
  final WebSocketService _wsService;
  final SecureStorage _storage;
  StreamSubscription? _wsSub;

  void _init() { _wsSub = _wsService.eventStream.listen(_onWsEvent); }

  void _onWsEvent(WsEvent event) {
    switch (event.type) {
      case WsEventType.conversationActivity:
        if (event.inbound == true && event.conversationId != null) {
          state = state.copyWith(unreadIds: {...state.unreadIds, event.conversationId!});
          refresh();
        }
        break;
      case WsEventType.conversationHandoff:
        refresh();
        break;
      default: break;
    }
  }

  Future<void> refresh() async {
    state = state.copyWith(status: ChatListStatus.refreshing, errorMessage: null);
    try {
      final conversations = await _repo.listConversations(offset: 0, limit: AppConfig.pageSize);
      _sortConversations(conversations);
      state = ChatListState(
        conversations: conversations, status: ChatListStatus.idle,
        hasMore: conversations.length >= AppConfig.pageSize, offset: conversations.length,
        modeFilter: state.modeFilter, searchQuery: state.searchQuery, unreadIds: state.unreadIds,
      );
    } on Failure catch (e) { state = state.copyWith(status: ChatListStatus.error, errorMessage: e.message); }
  }

  Future<void> loadMore() async {
    if (!state.hasMore || state.status == ChatListStatus.loadingMore) return;
    state = state.copyWith(status: ChatListStatus.loadingMore);
    try {
      final more = await _repo.listConversations(offset: state.offset, limit: AppConfig.pageSize);
      _sortConversations(more);
      state = state.copyWith(
        conversations: [...state.conversations, ...more], status: ChatListStatus.idle,
        hasMore: more.length >= AppConfig.pageSize, offset: state.offset + more.length,
      );
    } on Failure catch (e) { state = state.copyWith(status: ChatListStatus.error, errorMessage: e.message); }
  }

  void setSearch(String? query) { state = state.copyWith(searchQuery: query); }
  void setModeFilter(String? mode) { state = state.copyWith(modeFilter: mode); }
  void markRead(int conversationId) { state = state.copyWith(unreadIds: state.unreadIds.where((id) => id != conversationId).toSet()); }

  void _sortConversations(List<Conversation> list) {
    list.sort((a, b) {
      final aLead = a.tag == tagSalesLead ? 1 : 0;
      final bLead = b.tag == tagSalesLead ? 1 : 0;
      if (aLead != bLead) return bLead - aLead;
      return b.updatedAt.compareTo(a.updatedAt);
    });
  }

  List<Conversation> get filtered {
    var result = state.conversations;
    if (state.modeFilter != null) { result = result.where((c) => c.mode == state.modeFilter).toList(); }
    if (state.searchQuery != null && state.searchQuery!.isNotEmpty) {
      final q = state.searchQuery!.toLowerCase();
      result = result.where((c) => c.id.toString().contains(q) || c.assignedOperator?.toLowerCase().contains(q) == true || c.tag?.toLowerCase().contains(q) == true).toList();
    }
    return result;
  }

  @override
  void dispose() { _wsSub?.cancel(); super.dispose(); }
}

final chatListProvider = StateNotifierProvider<ChatListNotifier, ChatListState>((ref) {
  return ChatListNotifier(ref.watch(chatRepositoryProvider), ref.watch(webSocketServiceProvider), ref.watch(secureStorageProvider));
});
