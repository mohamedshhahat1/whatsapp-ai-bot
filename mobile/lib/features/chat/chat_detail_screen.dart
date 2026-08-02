import 'package:flutter/material.dart';
import 'package:flutter_animate/flutter_animate.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:pull_down_button/pull_down_button.dart';

import '../../core/theme/app_colors.dart';
import '../../core/utils/formatters.dart';
import '../../shared/widgets/loading_shimmer.dart';
import '../../shared/widgets/error_view.dart';
import 'chat_detail_provider.dart';
import 'chat_models.dart';
import 'widgets/message_bubble.dart';
import 'widgets/chat_composer.dart';
import 'widgets/handoff_banner.dart';

class ChatDetailScreen extends ConsumerStatefulWidget {
  final int conversationId;
  const ChatDetailScreen({super.key, required this.conversationId});
  @override
  ConsumerState<ChatDetailScreen> createState() => _ChatDetailScreenState();
}

class _ChatDetailScreenState extends ConsumerState<ChatDetailScreen> {
  final _scrollController = ScrollController();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      ref.read(chatDetailProvider(widget.conversationId).notifier).load(widget.conversationId);
    });
  }

  @override
  void dispose() { _scrollController.dispose(); super.dispose(); }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        _scrollController.animateTo(_scrollController.position.maxScrollExtent, duration: 300.ms, curve: Curves.easeOut);
      }
    });
  }

  /// Plain wording for the computed server-side state. The raw constants are
  /// meant for machines; 'WAITING_IDLE' tells an operator nothing about what
  /// is about to happen.
  String? _stateLabel(ConversationDetail detail) {
    switch (detail.sessionState) {
      case sessionWaitingIdle:
        // Only a countdown when the sweep is actually enabled. With
        // CONVERSATION_CLOSE_AFTER_IDLE off this is a resting state and
        // promising a close would be a lie.
        return (detail.closeAfterIdle ?? true)
            ? 'Quiet \u2014 closing after ${detail.idleTimeoutMinutes ?? '?'} min'
            : 'Quiet';
      case sessionClosing:
        return 'Sending closing message';
      case sessionClosed:
        return 'Session closed';
      default:
        return null;
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(chatDetailProvider(widget.conversationId));
    final notifier = ref.read(chatDetailProvider(widget.conversationId).notifier);
    final theme = Theme.of(context);
    final isDark = theme.brightness == Brightness.dark;
    final detail = state.detail;
    final closed = state.isClosed;

    ref.listen(chatDetailProvider(widget.conversationId), (prev, next) {
      if (next.detail?.messages.length != prev?.detail?.messages.length) { _scrollToBottom(); }
    });

    return Scaffold(
      backgroundColor: isDark ? AppColors.darkChatBg : AppColors.lightChatBg,
      appBar: AppBar(
        title: detail != null
            ? Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(detail.assignedOperator ?? 'Customer #${detail.userId}', style: theme.textTheme.titleMedium),
                Text(detail.mode == modeHuman ? 'Human Mode' : 'Bot Mode', style: theme.textTheme.bodySmall?.copyWith(color: detail.mode == modeHuman ? AppColors.humanMode : AppColors.botMode)),
              ])
            : const Text('Loading...'),
        actions: [
          if (detail != null && closed)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 14, horizontal: 4),
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 8),
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: theme.disabledColor.withValues(alpha: 0.18),
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Text('Closed', style: theme.textTheme.labelSmall),
              ),
            ),
          if (detail != null)
            IconButton(
              icon: const Icon(Icons.history),
              tooltip: 'Customer history',
              onPressed: () {
                notifier.loadHistory();
                _showHistory(context);
              },
            ),
          if (detail != null)
            PullDownButton(
              itemBuilder: (context) => [
                PullDownMenuItem(
                  // A closed session is still actionable: the backend reopens
                  // it. Saying so keeps the operator from assuming the button
                  // is broken when the conversation is marked Closed.
                  title: detail.mode == modeBot
                      ? (closed ? 'Reopen & Take Over' : 'Take Over')
                      : (closed ? 'Reopen & Resume AI' : 'Resume AI'),
                  icon: detail.mode == modeBot ? Icons.pan_tool : Icons.smart_toy,
                  enabled: state.canAct,
                  onTap: () { if (detail.mode == modeBot) { notifier.takeOver(); } else { notifier.resumeAi(); } },
                ),
                PullDownMenuItem(
                  title: 'Delete', icon: Icons.delete, isDestructive: true,
                  onTap: () async {
                    final confirmed = await _confirmDelete(context);
                    if (confirmed == true) {
                      final ok = await notifier.deleteConversation();
                      if (ok && context.mounted) context.go('/chats');
                    }
                  },
                ),
              ],
              buttonBuilder: (context, showMenu) => IconButton(icon: const Icon(Icons.more_vert), onPressed: showMenu),
            ),
        ],
      ),
      body: state.status == ChatDetailStatus.loading
          ? const Center(child: CircularProgressIndicator())
          : state.status == ChatDetailStatus.error && detail == null
              ? ErrorView(message: state.errorMessage ?? 'Failed to load', onRetry: () => notifier.load(widget.conversationId))
              : detail == null
                  ? const SizedBox()
                  : Column(children: [
                      if (detail.mode == modeHuman || detail.tag == tagSalesLead)
                        HandoffBanner(conversation: detail, onTakeOver: detail.mode == modeBot ? notifier.takeOver : null, onResumeAi: detail.mode == modeHuman ? notifier.resumeAi : null),
                      if (state.superseded)
                        _Banner(
                          color: AppColors.error,
                          icon: Icons.block,
                          text: 'This customer has already started a newer '
                              'conversation. This one can no longer be replied '
                              'to. Open their current conversation instead.',
                        )
                      else if (closed)
                        _Banner(
                          color: theme.disabledColor,
                          icon: Icons.lock_clock,
                          text: 'This session has ended. Replying reopens it, '
                              'so the transcript stays together and the '
                              'customer is not greeted again.',
                        )
                      else if (_stateLabel(detail) != null)
                        _Banner(
                          color: theme.disabledColor,
                          icon: Icons.schedule,
                          text: _stateLabel(detail)!,
                        ),
                      Expanded(
                        child: ListView.builder(
                          controller: _scrollController,
                          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                          itemCount: detail.messages.length,
                          itemBuilder: (context, index) {
                            final msg = detail.messages[index];
                            return MessageBubble(message: msg).animate().fadeIn(duration: 200.ms).slideY(begin: 0.1, duration: 200.ms);
                          },
                        ),
                      ),
                      // Swapped out rather than disabled, so ChatComposer
                      // keeps its existing signature. Note this applies only
                      // when SUPERSEDED -- a merely closed session keeps a
                      // working composer, because sending there reopens it.
                      if (state.superseded)
                        const _ComposerDisabled()
                      else
                        ChatComposer(onSend: (text) async { final ok = await notifier.sendReply(text); return ok; }, isSending: state.status == ChatDetailStatus.sending),
                    ]),
    );
  }

  /// The customer's other sessions.
  ///
  /// Sessions are deliberately never merged -- the gaps between them are the
  /// point of the lifecycle -- so this offers navigation and a count rather
  /// than a stitched-together transcript. Operator-only: none of it is sent
  /// to the model.
  void _showHistory(BuildContext context) {
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      builder: (sheetContext) => Consumer(
        builder: (context, ref, _) {
          final state = ref.watch(chatDetailProvider(widget.conversationId));
          final history = state.history;
          if (history == null) {
            return const SizedBox(
              height: 180,
              child: Center(child: CircularProgressIndicator()),
            );
          }
          final theme = Theme.of(context);
          return SafeArea(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    history.name?.isNotEmpty == true ? history.name! : history.waId,
                    style: theme.textTheme.titleMedium,
                  ),
                  Text(
                    '${history.totalConversations} '
                    '${history.totalConversations == 1 ? 'conversation' : 'conversations'} in total',
                    style: theme.textTheme.bodySmall,
                  ),
                  const SizedBox(height: 12),
                  if (history.previous.isEmpty)
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 12),
                      child: Text('This is their first conversation.',
                          style: theme.textTheme.bodySmall),
                    )
                  else
                    Flexible(
                      child: ListView.builder(
                        shrinkWrap: true,
                        itemCount: history.previous.length,
                        itemBuilder: (context, index) {
                          final previous = history.previous[index];
                          return ListTile(
                            dense: true,
                            leading: Icon(
                              previous.status == statusClosed
                                  ? Icons.lock_outline
                                  : Icons.chat_bubble_outline,
                              size: 18,
                            ),
                            title: Text('#${previous.id}'),
                            subtitle: Text(
                              formatDateTime(previous.createdAt),
                              style: theme.textTheme.bodySmall,
                            ),
                            trailing: previous.tag == tagSalesLead
                                ? const Icon(Icons.sell_outlined, size: 16)
                                : null,
                            onTap: () {
                              Navigator.pop(sheetContext);
                              context.go('/chats/${previous.id}');
                            },
                          );
                        },
                      ),
                    ),
                ],
              ),
            ),
          );
        },
      ),
    );
  }

  Future<bool?> _confirmDelete(BuildContext context) {
    return showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete conversation?'),
        content: const Text('This will permanently delete the conversation and all its messages. This cannot be undone.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), style: FilledButton.styleFrom(backgroundColor: AppColors.error), child: const Text('Delete')),
        ],
      ),
    );
  }
}

class _Banner extends StatelessWidget {
  const _Banner({required this.color, required this.icon, required this.text});
  final Color color;
  final IconData icon;
  final String text;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      color: color.withValues(alpha: 0.12),
      child: Row(children: [
        Icon(icon, size: 16, color: color),
        const SizedBox(width: 8),
        Expanded(child: Text(text, style: theme.textTheme.bodySmall)),
      ]),
    );
  }
}

class _ComposerDisabled extends StatelessWidget {
  const _ComposerDisabled();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return SafeArea(
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
        color: theme.disabledColor.withValues(alpha: 0.08),
        child: Text(
          'Replying is disabled \u2014 this conversation has been superseded '
          'by a newer one.',
          textAlign: TextAlign.center,
          style: theme.textTheme.bodySmall,
        ),
      ),
    );
  }
}
