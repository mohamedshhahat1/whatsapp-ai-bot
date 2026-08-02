import 'package:flutter/material.dart';
import 'package:flutter_animate/flutter_animate.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/utils/formatters.dart';
import '../chat_models.dart';

class ConversationTile extends StatelessWidget {
  final Conversation conversation;
  final bool isUnread;
  final bool isLead;
  final VoidCallback onTap;

  const ConversationTile({super.key, required this.conversation, required this.isUnread, required this.isLead, required this.onTap});

  /// Short label for a session that is not plainly active. Returns null for
  /// the ordinary case so the common tile stays uncluttered -- a badge on
  /// every row would carry no information.
  String? _stateLabel() {
    if (conversation.status == statusClosed) return 'Closed';
    switch (conversation.sessionState) {
      case sessionWaitingIdle:
        return 'Quiet';
      case sessionClosing:
        return 'Closing';
      default:
        return null;
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isHuman = conversation.mode == modeHuman;
    final isClosed = conversation.status == statusClosed;
    final stateLabel = _stateLabel();
    // A closed lead is history, not a queue item. Keeping the gold highlight
    // on it would compete for attention with customers actually waiting.
    final highlightLead = isLead && !isClosed;
    return InkWell(
      onTap: onTap,
      child: Opacity(
        // Dimming the whole row is what makes a closed session legible while
        // scrolling; a badge alone is only noticed once you stop to read.
        opacity: isClosed ? 0.62 : 1.0,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
          decoration: BoxDecoration(color: highlightLead ? AppColors.goldGlow.withOpacity(0.15) : null, border: Border(bottom: BorderSide(color: theme.dividerColor, width: 0.5))),
          child: Row(
            children: [
              Stack(children: [
                CircleAvatar(radius: 28, backgroundColor: highlightLead ? AppColors.gold : AppColors.primary, child: Text(Formatters.initials(conversation.assignedOperator, '?'), style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600))),
                if (highlightLead)
                  Positioned(right: 0, bottom: 0, child: Container(padding: const EdgeInsets.all(3), decoration: const BoxDecoration(color: Colors.white, shape: BoxShape.circle), child: const Icon(Icons.star, size: 12, color: AppColors.gold))).animate().shimmer(duration: 1500.ms),
              ]),
              const SizedBox(width: 12),
              Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Row(children: [
                  Expanded(child: Text(conversation.assignedOperator ?? 'Customer #${conversation.userId}', style: theme.textTheme.titleMedium?.copyWith(fontWeight: isUnread ? FontWeight.w700 : FontWeight.w500), maxLines: 1, overflow: TextOverflow.ellipsis)),
                  Text(Formatters.chatTime(conversation.updatedAt), style: theme.textTheme.bodySmall?.copyWith(color: isUnread ? AppColors.primary : null, fontWeight: isUnread ? FontWeight.w600 : null)),
                ]),
                const SizedBox(height: 4),
                Row(children: [
                  Container(padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2), decoration: BoxDecoration(color: isHuman ? AppColors.humanMode.withOpacity(0.15) : AppColors.botMode.withOpacity(0.15), borderRadius: BorderRadius.circular(6)), child: Row(mainAxisSize: MainAxisSize.min, children: [
                    Icon(isHuman ? Icons.person : Icons.smart_toy, size: 10, color: isHuman ? AppColors.humanMode : AppColors.botMode),
                    const SizedBox(width: 3),
                    Text(isHuman ? 'Human' : 'Bot', style: TextStyle(fontSize: 10, fontWeight: FontWeight.w600, color: isHuman ? AppColors.humanMode : AppColors.botMode)),
                  ])),
                  if (isLead) ...[
                    const SizedBox(width: 4),
                    Container(padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2), decoration: BoxDecoration(color: AppColors.gold.withOpacity(0.2), borderRadius: BorderRadius.circular(6)), child: const Row(mainAxisSize: MainAxisSize.min, children: [Icon(Icons.star, size: 10, color: AppColors.gold), SizedBox(width: 3), Text('Lead', style: TextStyle(fontSize: 10, fontWeight: FontWeight.w700, color: AppColors.gold))])),
                  ],
                  if (stateLabel != null) ...[
                    const SizedBox(width: 4),
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                      decoration: BoxDecoration(
                        color: theme.disabledColor.withOpacity(0.18),
                        borderRadius: BorderRadius.circular(6),
                      ),
                      child: Row(mainAxisSize: MainAxisSize.min, children: [
                        Icon(isClosed ? Icons.lock_outline : Icons.schedule,
                            size: 10, color: theme.disabledColor),
                        const SizedBox(width: 3),
                        Text(stateLabel, style: TextStyle(fontSize: 10, fontWeight: FontWeight.w600, color: theme.disabledColor)),
                      ]),
                    ),
                  ],
                  const Spacer(),
                ]),
              ])),
              if (isUnread)
                Container(padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2), decoration: BoxDecoration(color: AppColors.primary, borderRadius: BorderRadius.circular(12)), child: const Text('\u25cf', style: TextStyle(color: Colors.white, fontSize: 10))).animate().scale(),
            ],
          ),
        ),
      ),
    );
  }
}
