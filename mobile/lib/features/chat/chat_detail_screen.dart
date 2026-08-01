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

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(chatDetailProvider(widget.conversationId));
    final notifier = ref.read(chatDetailProvider(widget.conversationId).notifier);
    final theme = Theme.of(context);
    final isDark = theme.brightness == Brightness.dark;
    final detail = state.detail;

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
          if (detail != null)
            PullDownButton(
              itemBuilder: (context) => [
                PullDownMenuItem(
                  title: detail.mode == modeBot ? 'Take Over' : 'Resume AI',
                  icon: detail.mode == modeBot ? Icons.pan_tool : Icons.smart_toy,
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
                      ChatComposer(onSend: (text) async { final ok = await notifier.sendReply(text); return ok; }, isSending: state.status == ChatDetailStatus.sending),
                    ]),
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
