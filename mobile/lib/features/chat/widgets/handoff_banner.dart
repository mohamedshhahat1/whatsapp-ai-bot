import 'package:flutter/material.dart';
import 'package:flutter_animate/flutter_animate.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/utils/formatters.dart';
import '../chat_models.dart';

class HandoffBanner extends StatelessWidget {
  final ConversationDetail conversation;
  final Future<bool> Function()? onTakeOver;
  final Future<bool> Function()? onResumeAi;
  const HandoffBanner({super.key, required this.conversation, this.onTakeOver, this.onResumeAi});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isHuman = conversation.mode == modeHuman;
    final isLead = conversation.tag == tagSalesLead;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      decoration: BoxDecoration(
        gradient: LinearGradient(colors: isLead
            ? [AppColors.gold.withOpacity(0.15), AppColors.goldGlow.withOpacity(0.05)]
            : isHuman
                ? [AppColors.humanMode.withOpacity(0.15), AppColors.humanMode.withOpacity(0.05)]
                : [AppColors.botMode.withOpacity(0.15), AppColors.botMode.withOpacity(0.05)]),
        border: Border(bottom: BorderSide(color: theme.dividerColor, width: 0.5)),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Icon(isHuman ? Icons.pan_tool : Icons.smart_toy, size: 16, color: isHuman ? AppColors.humanMode : AppColors.botMode),
          const SizedBox(width: 6),
          Text(isHuman ? 'Human Mode' : 'Bot Mode', style: theme.textTheme.labelSmall?.copyWith(color: isHuman ? AppColors.humanMode : AppColors.botMode, fontWeight: FontWeight.w700)),
          if (isLead) ...[
            const SizedBox(width: 8),
            Container(padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2), decoration: BoxDecoration(color: AppColors.gold.withOpacity(0.2), borderRadius: BorderRadius.circular(6)), child: const Row(mainAxisSize: MainAxisSize.min, children: [Icon(Icons.star, size: 10, color: AppColors.gold), SizedBox(width: 3), Text('Sales Lead', style: TextStyle(fontSize: 10, fontWeight: FontWeight.w700, color: AppColors.gold))])).animate().shimmer(duration: 2000.ms),
          ],
          const Spacer(),
          if (conversation.assignedOperator != null) Text(conversation.assignedOperator!, style: theme.textTheme.bodySmall),
        ]),
        if (conversation.handoffAt != null) ...[
          const SizedBox(height: 4),
          Text('Handoff: ${Formatters.fullDate(conversation.handoffAt!)}', style: theme.textTheme.bodySmall),
        ],
        if (onTakeOver != null || onResumeAi != null) ...[
          const SizedBox(height: 8),
          Row(children: [
            if (onTakeOver != null)
              FilledButton.icon(onPressed: () => onTakeOver!(), icon: const Icon(Icons.pan_tool, size: 16), label: const Text('Take Over'), style: FilledButton.styleFrom(backgroundColor: AppColors.humanMode, padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6), textStyle: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600))).animate().fadeIn(),
            if (onResumeAi != null) ...[
              const SizedBox(width: 8),
              OutlinedButton.icon(onPressed: () => onResumeAi!(), icon: const Icon(Icons.smart_toy, size: 16), label: const Text('Resume AI'), style: OutlinedButton.styleFrom(padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6), textStyle: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600))).animate().fadeIn(),
            ],
          ]),
        ],
      ]),
    );
  }
}
