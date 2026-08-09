import 'package:flutter/material.dart';
import 'package:flutter_animate/flutter_animate.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme/app_colors.dart';
import '../../core/utils/formatters.dart';
import 'notification_provider.dart';

class NotificationsScreen extends ConsumerWidget {
  const NotificationsScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final notifications = ref.watch(notificationProvider);
    final notifier = ref.read(notificationProvider.notifier);
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text('Notifications', style: theme.textTheme.titleLarge),
        actions: [if (notifications.isNotEmpty) TextButton(onPressed: () => notifier.markAllRead(), child: const Text('Mark all read'))],
      ),
      body: notifications.isEmpty
          ? Center(child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
              Icon(Icons.notifications_none, size: 64, color: theme.colorScheme.onSurfaceVariant.withOpacity(0.3)).animate().scale(duration: 600.ms),
              const SizedBox(height: 16),
              Text('No notifications', style: theme.textTheme.titleMedium),
              const SizedBox(height: 8),
              Text('You will be notified when new events arrive', style: theme.textTheme.bodySmall),
            ]))
          : ListView.builder(
              itemCount: notifications.length,
              itemBuilder: (context, index) {
                final n = notifications[index];
                final isLead = n.tag == 'sales_lead';
                return Dismissible(
                  key: ValueKey(n.id),
                  direction: DismissDirection.endToStart,
                  background: Container(color: AppColors.error, alignment: Alignment.centerRight, padding: const EdgeInsets.only(right: 16), child: const Icon(Icons.delete, color: Colors.white)),
                  onDismissed: (_) => notifier.remove(n.id),
                  child: ListTile(
                    onTap: () { notifier.markRead(n.id); if (n.conversationId != null) context.push('/chats/${n.conversationId}'); },
                    leading: CircleAvatar(backgroundColor: isLead ? AppColors.gold : AppColors.primary, child: Icon(isLead ? Icons.star : Icons.notifications, color: Colors.white, size: 18)),
                    title: Row(children: [
                      if (!n.isRead) Container(width: 8, height: 8, margin: const EdgeInsets.only(right: 6), decoration: const BoxDecoration(color: AppColors.primary, shape: BoxShape.circle)),
                      Expanded(child: Text(n.title, style: TextStyle(fontWeight: n.isRead ? FontWeight.w400 : FontWeight.w700))),
                    ]),
                    subtitle: Text(n.body, maxLines: 2, overflow: TextOverflow.ellipsis),
                    trailing: Text(Formatters.chatTime(n.timestamp.toIso8601String()), style: theme.textTheme.bodySmall),
                  ).animate().fadeIn(duration: 300.ms).slideX(begin: -0.05),
                );
              },
            ),
    );
  }
}
