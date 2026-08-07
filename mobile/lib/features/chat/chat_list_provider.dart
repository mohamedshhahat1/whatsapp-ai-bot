import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/config/app_config.dart';
import '../../core/error/failures.dart';
import '../../core/websocket/websocket_service.dart';
import 'chat_models.dart';
import 'chat_repository.dart';

enum ChatListStatus { idle, loading, refreshing, loadingMore, error }

/// Sentinel default for [ChatListState.copyWith].
///
/// Every nullable filter below used to be written `x ?? this.x`, which meant
/// passing null preserved the old value instead of clearing it -- so "All",
/// "All sessions" and closing the search box all did nothing, and every
/// chip's onDeleted was inert. With a sentinel, omitting an argument
/// preserves and passing null clears, which is what the callers always meant.
const Object _unchanged = Object();

class ChatListState {
  final List<Conversation> conversations;
  final ChatListStatus status;
  final String? errorMessage;
  final bool hasMore;
  final int offset;
  final String? searchQuery;
  final String? modeFilter;
  /// 'active', 'closed', or null for every session. Applied server-side so
  /// paging stays correct -- filtering a page after fetching it would return
  /// short pages and break the hasMore calculation.
  final String? statusFilter;
  /// A channel constant, or null for every channel. Applied client-side,
  /// unlike [statusFilter], because the list endpoint has no channel
  /// parameter to pass it to. That means it carries the same caveat as
  /// [modeFilter]: it narrows the rows already loaded rather than asking the
  /// server for a full page of matches.
  final String? channelFilter;
  final Set<int> unreadIds;

  const ChatListState({
    this.conversations = const [], this.status = ChatListStatus.idle,
    this.errorMessage, this.hasMore = true, this.offset = 0,
    this.searchQuery, this.modeFilter, this.statusFilter, this.channelFilter,
    this.unreadIds = const {},
  });

  ChatListState copyWith({
    List<Conversation>? conversations, ChatListStatus? status, String? errorMessage,
    bool? hasMore, int? offset,
    Object? searchQuery = _unchanged,
    Object? modeFilter = _unchanged,
    Object? statusFilter = _unchanged,
    Object? channelFilter = _unchanged,
    Set<int>? unreadIds,
  }) => ChatListState(
    conversations: conversations ?? this.conversations, status: status ?? this.status,
    errorMessage: errorMessage, hasMore: hasMore ?? this.hasMore, offset: offset ?? this.offset,
    searchQuery: identical(searchQuery, _unchanged) ? this.searchQuery : searchQuery as String?,
    modeFilter: identical(modeFilter, _unchanged) ? this.modeFilter : modeFilter as String?,
    statusFilter: identical(statusFilter, _unchanged) ? this.statusFilter : statusFilter as String?,
    channelFilter: identical(channelFilter, _unchanged) ? this.channelFilter : channelFilter as String?,
    unreadIds: unreadIds ?? this.unreadIds,
  );
}

class ChatListNotifier extends StateNotifier<ChatListState> {
  ChatListNotifier(this._repo, this._wsService) : super(const ChatListState()) { _init(); }

  final ChatRepository _repo;
  final WebSocketService _wsService;
  StreamSubscription<WsEvent>? _wsSub;

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
      // A session closing or reopening changes the badge, the ordering and
      // whether the row belongs in the current filter at all. Without these
      // the list showed sessions as active indefinitely after the sweep had
      // ended them.
      //
      // Deliberately NOT marked unread, unlike an inbound message: a session
      // timing out is not something the customer said, and lighting up an
      // unread badge for it would send operators to conversations where
      // nobody is waiting.
      case WsEventType.conversationClosed:
      case WsEventType.conversationReopened:
        refresh();
        break;
      default: break;
    }
  }

  Future<void> refresh() async {
    state = state.copyWith(status: ChatListStatus.refreshing, errorMessage: null);
    try {
      final conversations = await _repo.listConversations(
        offset: 0, limit: AppConfig.pageSize, status: state.statusFilter,
      );
      _sortConversations(conversations);
      state = ChatListState(
        conversations: conversations, status: ChatListStatus.idle,
        hasMore: conversations.length >= AppConfig.pageSize, offset: conversations.length,
        modeFilter: state.modeFilter, searchQuery: state.searchQuery,
        statusFilter: state.statusFilter, channelFilter: state.channelFilter,
        unreadIds: state.unreadIds,
      );
    } on Failure catch (e) { state = state.copyWith(status: ChatListStatus.error, errorMessage: e.message); }
  }

  Future<void> loadMore() async {
    if (!state.hasMore || state.status == ChatListStatus.loadingMore) return;
    state = state.copyWith(status: ChatListStatus.loadingMore);
    try {
      final more = await _repo.listConversations(
        offset: state.offset, limit: AppConfig.pageSize, status: state.statusFilter,
      );
      _sortConversations(more);
      state = state.copyWith(
        conversations: [...state.conversations, ...more], status: ChatListStatus.idle,
        hasMore: more.length >= AppConfig.pageSize, offset: state.offset + more.length,
      );
    } on Failure catch (e) { state = state.copyWith(status: ChatListStatus.error, errorMessage: e.message); }
  }

  void setSearch(String? query) { state = state.copyWith(searchQuery: query); }
  void setModeFilter(String? mode) { state = state.copyWith(modeFilter: mode); }

  /// Filters by lifecycle status. Refetches because the filter is applied
  /// server-side; filtering the loaded page locally would silently drop rows
  /// that paging has not reached yet.
  void setStatusFilter(String? status) {
    state = state.copyWith(statusFilter: status, offset: 0);
    refresh();
  }

  /// Filters by originating channel.
  ///
  /// No refetch, because there is no channel parameter on the list endpoint
  /// to refetch with -- this narrows what is already loaded, exactly as the
  /// mode filter does.
  void setChannelFilter(String? channel) {
    state = state.copyWith(channelFilter: channel);
  }

  /// The channels present in the conversations currently loaded, in the
  /// canonical order from [allChannels].
  ///
  /// The filter menu offers a Channel section only when this has more than
  /// one entry. Deriving it from loaded rows rather than from [allChannels]
  /// means the menu describes this deployment: with only WhatsApp enabled --
  /// the default -- no channel section appears at all, instead of four
  /// options that can never match anything.
  ///
  /// The limitation is the same one the filter itself has: this sees loaded
  /// pages only, so a channel that first appears deep in the history is not
  /// offered until paging reaches it.
  List<String> get availableChannels {
    int rank(String channel) {
      final index = allChannels.indexOf(channel);
      return index == -1 ? allChannels.length : index;
    }

    final seen = state.conversations.map((c) => c.channel).toSet().toList();
    seen.sort((a, b) {
      final byRank = rank(a).compareTo(rank(b));
      return byRank != 0 ? byRank : a.compareTo(b);
    });
    return seen;
  }

  void markRead(int conversationId) { state = state.copyWith(unreadIds: state.unreadIds.where((id) => id != conversationId).toSet()); }

  /// Unclaimed sales leads first, then most recently updated.
  ///
  /// The lead pin applies only to ACTIVE sessions. It used to apply to all of
  /// them, and because the sales_lead tag is sticky -- it records what the
  /// conversation turned out to be, not who is answering it -- a closed lead
  /// from last week sat permanently above a live customer waiting right now,
  /// with nothing an operator could do to shift it. This matches the
  /// backend's _UNCLAIMED_LEAD ordering, so the two agree about what belongs
  /// at the top.
  void _sortConversations(List<Conversation> list) {
    list.sort((a, b) {
      final aLead = (a.tag == tagSalesLead && a.status == statusActive) ? 1 : 0;
      final bLead = (b.tag == tagSalesLead && b.status == statusActive) ? 1 : 0;
      if (aLead != bLead) return bLead - aLead;
      return b.updatedAt.compareTo(a.updatedAt);
    });
  }

  List<Conversation> get filtered {
    var result = state.conversations;
    if (state.modeFilter != null) { result = result.where((c) => c.mode == state.modeFilter).toList(); }
    if (state.channelFilter != null) { result = result.where((c) => c.channel == state.channelFilter).toList(); }
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
  return ChatListNotifier(ref.watch(chatRepositoryProvider), ref.watch(webSocketServiceProvider));
});
