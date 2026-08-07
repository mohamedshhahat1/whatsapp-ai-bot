import 'package:flutter/material.dart';
import 'package:flutter_animate/flutter_animate.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_slidable/flutter_slidable.dart';
import 'package:go_router/go_router.dart';
import 'package:pull_down_button/pull_down_button.dart';

import '../../core/theme/app_colors.dart';
import '../../core/utils/formatters.dart';
import '../../shared/widgets/loading_shimmer.dart';
import '../../shared/widgets/error_view.dart';
import 'chat_list_provider.dart';
import 'chat_models.dart';
import 'widgets/channel_badge.dart';
import 'widgets/conversation_tile.dart';

class ChatListScreen extends ConsumerStatefulWidget {
  const ChatListScreen({super.key});
  @override
  ConsumerState<ChatListScreen> createState() => _ChatListScreenState();
}

class _ChatListScreenState extends ConsumerState<ChatListScreen> {
  final _scrollController = ScrollController();
  final _searchController = TextEditingController();
  bool _showSearch = false;

  @override
  void initState() {
    super.initState();
    _scrollController.addListener(_onScroll);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      ref.read(chatListProvider.notifier).refresh();
    });
  }

  @override
  void dispose() {
    _scrollController.dispose();
    _searchController.dispose();
    super.dispose();
  }

  void _onScroll() {
    if (_scrollController.position.pixels >= _scrollController.position.maxScrollExtent - 200) {
      ref.read(chatListProvider.notifier).loadMore();
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(chatListProvider);
    final notifier = ref.read(chatListProvider.notifier);
    final filtered = notifier.filtered;
    // Only offered once the loaded rows actually span more than one channel.
    // See ChatListNotifier.availableChannels.
    final channels = notifier.availableChannels;
    final hasChips = state.modeFilter != null ||
        state.statusFilter != null ||
        state.channelFilter != null;
    // Whether ANY narrowing is in effect, which decides what the empty state
    // should say. Without this it blamed the status filter for an empty list
    // that a channel filter or a search had caused.
    final isNarrowed = hasChips || (state.searchQuery?.isNotEmpty ?? false);

    return Scaffold(
      body: CustomScrollView(
        controller: _scrollController,
        slivers: [
          SliverAppBar(
            pinned: true,
            expandedHeight: _showSearch ? 120 : 64,
            title: Text('Chats', style: Theme.of(context).textTheme.titleLarge),
            actions: [
              IconButton(
                icon: Icon(_showSearch ? Icons.close : Icons.search),
                onPressed: () => setState(() {
                  _showSearch = !_showSearch;
                  if (!_showSearch) { _searchController.clear(); notifier.setSearch(null); }
                }),
              ),
              PullDownButton(
                itemBuilder: (context) => [
                  const PullDownMenuTitle(title: Text('Answered by')),
                  PullDownMenuItem(title: 'All', icon: Icons.all_inclusive, onTap: () => notifier.setModeFilter(null)),
                  PullDownMenuItem(title: 'Bot Mode', icon: Icons.smart_toy, onTap: () => notifier.setModeFilter('bot')),
                  PullDownMenuItem(title: 'Human Mode', icon: Icons.person, onTap: () => notifier.setModeFilter('human')),
                  const PullDownMenuDivider.large(),
                  // Sessions close on their own, so 'closed' is an ordinary
                  // and common state rather than an archive. Operators
                  // normally want the active ones only.
                  const PullDownMenuTitle(title: Text('Session')),
                  PullDownMenuItem(title: 'All sessions', icon: Icons.all_inclusive, onTap: () => notifier.setStatusFilter(null)),
                  PullDownMenuItem(title: 'Active only', icon: Icons.chat_bubble_outline, onTap: () => notifier.setStatusFilter(statusActive)),
                  PullDownMenuItem(title: 'Closed only', icon: Icons.lock_outline, onTap: () => notifier.setStatusFilter(statusClosed)),
                  if (channels.length > 1) ...[
                    const PullDownMenuDivider.large(),
                    const PullDownMenuTitle(title: Text('Channel')),
                    PullDownMenuItem(title: 'All channels', icon: Icons.all_inclusive, onTap: () => notifier.setChannelFilter(null)),
                    for (final channel in channels)
                      PullDownMenuItem(
                        title: ChannelDisplay.of(channel).label,
                        icon: ChannelDisplay.of(channel).icon,
                        onTap: () => notifier.setChannelFilter(channel),
                      ),
                  ],
                ],
                buttonBuilder: (context, showMenu) => IconButton(icon: const Icon(Icons.filter_list), onPressed: showMenu),
              ),
            ],
          ),
          if (_showSearch)
            SliverToBoxAdapter(
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                child: TextField(
                  controller: _searchController,
                  decoration: const InputDecoration(hintText: 'Search conversations...', prefixIcon: Icon(Icons.search, size: 20), isDense: true),
                  onChanged: notifier.setSearch,
                ).animate().fadeIn().slideY(begin: -0.1),
              ),
            ),
          if (hasChips)
            SliverToBoxAdapter(
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                child: Wrap(spacing: 8, children: [
                  if (state.modeFilter != null)
                    FilterChip(
                      label: Text(state.modeFilter == 'bot' ? 'Bot Mode' : 'Human Mode'),
                      onSelected: (_) => notifier.setModeFilter(null),
                      onDeleted: () => notifier.setModeFilter(null),
                      selected: true,
                    ),
                  if (state.statusFilter != null)
                    FilterChip(
                      label: Text(state.statusFilter == statusClosed ? 'Closed only' : 'Active only'),
                      onSelected: (_) => notifier.setStatusFilter(null),
                      onDeleted: () => notifier.setStatusFilter(null),
                      selected: true,
                    ),
                  if (state.channelFilter != null)
                    FilterChip(
                      avatar: Icon(
                        ChannelDisplay.of(state.channelFilter!).icon,
                        size: 14,
                        color: ChannelDisplay.of(state.channelFilter!).color,
                      ),
                      label: Text(ChannelDisplay.of(state.channelFilter!).label),
                      onSelected: (_) => notifier.setChannelFilter(null),
                      onDeleted: () => notifier.setChannelFilter(null),
                      selected: true,
                    ),
                ]),
              ),
            ),
          if (state.status == ChatListStatus.refreshing && state.conversations.isEmpty)
            SliverToBoxAdapter(child: const ChatListShimmer())
          else if (state.status == ChatListStatus.error && state.conversations.isEmpty)
            SliverToBoxAdapter(child: ErrorView(message: state.errorMessage ?? 'Failed to load', onRetry: () => notifier.refresh()))
          else if (filtered.isEmpty)
            SliverToBoxAdapter(
              child: _EmptyState(
                icon: Icons.chat_bubble_outline,
                title: isNarrowed ? 'No matching conversations' : 'No conversations',
                subtitle: isNarrowed
                    ? 'Nothing matches the filters in effect. Clear them to see the rest.'
                    : 'Conversations will appear here when customers message your bot.',
              ),
            )
          else
            SliverList.builder(
              itemCount: filtered.length + (state.hasMore ? 1 : 0),
              itemBuilder: (context, index) {
                if (index >= filtered.length) {
                  return const Padding(padding: EdgeInsets.all(16), child: Center(child: CircularProgressIndicator()));
                }
                final conv = filtered[index];
                final isUnread = state.unreadIds.contains(conv.id);
                final isLead = conv.tag == tagSalesLead;
                return Slidable(
                  // One customer has many sessions over time, so this key must
                  // be the conversation id, never the user id.
                  key: ValueKey(conv.id),
                  endActionPane: ActionPane(
                    motion: const DrawerMotion(),
                    children: [
                      SlidableAction(onPressed: (_) => context.push('/chats/${conv.id}'), backgroundColor: AppColors.primary, foregroundColor: Colors.white, icon: Icons.open_in_new, label: 'Open'),
                      // markRead returns void; awaiting it was a compile
                      // error, since a void expression has no value to use.
                      SlidableAction(onPressed: (_) => notifier.markRead(conv.id), backgroundColor: AppColors.info, foregroundColor: Colors.white, icon: Icons.mark_chat_read, label: 'Read'),
                    ],
                  ),
                  child: ConversationTile(
                    conversation: conv,
                    isUnread: isUnread,
                    isLead: isLead,
                    onTap: () { notifier.markRead(conv.id); context.push('/chats/${conv.id}'); },
                  ),
                ).animate().fadeIn(duration: 300.ms).slideY(begin: 0.05);
              },
            ),
        ],
      ),
      floatingActionButton: FloatingActionButton(
        onPressed: () => notifier.refresh(),
        child: const Icon(Icons.refresh),
      ).animate().scale(delay: 500.ms),
    );
  }
}

class _EmptyState extends StatelessWidget {
  final IconData icon;
  final String title;
  final String subtitle;
  const _EmptyState({required this.icon, required this.title, required this.subtitle});
  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.all(48),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(icon, size: 64, color: theme.colorScheme.onSurfaceVariant.withOpacity(0.3)).animate().scale(duration: 600.ms),
          const SizedBox(height: 16),
          Text(title, style: theme.textTheme.titleMedium),
          const SizedBox(height: 8),
          Text(subtitle, textAlign: TextAlign.center, style: theme.textTheme.bodySmall),
        ],
      ),
    );
  }
}
