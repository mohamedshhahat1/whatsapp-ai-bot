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

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isHuman = conversation.mode == modeHuman;
    return InkWell(
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        decoration: BoxDecoration(color: isLead ? AppColors.goldGlow.withOpacity(0.15) : null, border: Border(bottom: BorderSide(color: theme.dividerColor, width: 0.5))),
        child: Row(
          children: [
            Stack(children: [
              CircleAvatar(radius: 28, backgroundColor: isLead ? AppColors.gold : AppColors.primary, child: Text(Formatters.initials(conversation.assignedOperator, '?'), style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600))),
              if (isLead)
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
                  Container(padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2), decoration: BoxDecoration(color: AppColors.gold.withOpacity(0.2), borderRadius: BorderRadius.circular(6)), child: const Row(mainAxisSize: MainAxisSize.min, children: [Icon(Icons.star, size: 10, color: AppColors.gold), SizedBox(width: 3), Text('Lead', style: TextStyle(fontSize: 10, fontWeight: FontWeight.w700, color: AppColors.gold))])).animate().shimmer(duration: 2000.ms),
                ],
                const SizedBox(width: 8),
                Expanded(child: Text(conversation.status, style: theme.textTheme.bodySmall, maxLines: 1, overflow: TextOverflow.ellipsis)),
              ]),
            ])),
            if (isUnread)
              Container(padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2), decoration: BoxDecoration(color: AppColors.primary, borderRadius: BorderRadius.circular(12)), child: const Text('●', style: TextStyle(color: Colors.white, fontSize: 10))).animate().scale(),
          ],
        ),
      ),
    );
  }
}
