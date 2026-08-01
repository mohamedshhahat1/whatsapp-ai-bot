import 'package:flutter/material.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/utils/formatters.dart';
import '../chat_models.dart';

class MessageBubble extends StatelessWidget {
  final Message message;
  const MessageBubble({super.key, required this.message});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isDark = theme.brightness == Brightness.dark;
    final isOutbound = message.direction == dirOutbound;
    final bgColor = isOutbound ? (isDark ? AppColors.outBubbleDark : AppColors.outBubbleLight) : (isDark ? AppColors.inBubbleDark : AppColors.inBubbleLight);
    final textColor = isDark ? Colors.white : Colors.black87;

    return Align(
      alignment: isOutbound ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.75),
        margin: const EdgeInsets.symmetric(vertical: 3),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        decoration: BoxDecoration(
          color: bgColor,
          borderRadius: BorderRadius.only(
            topLeft: const Radius.circular(16), topRight: const Radius.circular(16),
            bottomLeft: isOutbound ? const Radius.circular(16) : Radius.zero,
            bottomRight: isOutbound ? Radius.zero : const Radius.circular(16),
          ),
          boxShadow: [BoxShadow(color: Colors.black.withOpacity(0.05), blurRadius: 2, offset: const Offset(0, 1))],
        ),
        child: Column(crossAxisAlignment: CrossAxisAlignment.end, children: [
          if (message.type == 'text')
            Text(message.content ?? '', style: TextStyle(color: textColor, fontSize: 15))
          else if (message.type == 'image')
            _MediaPlaceholder(icon: Icons.image, label: 'Image', color: textColor)
          else if (message.type == 'document')
            _MediaPlaceholder(icon: Icons.description, label: 'Document', color: textColor)
          else if (message.type == 'audio')
            _MediaPlaceholder(icon: Icons.mic, label: 'Voice Message', color: textColor)
          else if (message.type == 'location')
            _MediaPlaceholder(icon: Icons.location_on, label: 'Location', color: textColor)
          else if (message.type == 'video')
            _MediaPlaceholder(icon: Icons.videocam, label: 'Video', color: textColor)
          else
            Text(message.content ?? message.type, style: TextStyle(color: textColor, fontSize: 15)),
          const SizedBox(height: 4),
          Row(mainAxisSize: MainAxisSize.min, children: [
            Text(Formatters.messageTime(message.createdAt), style: TextStyle(fontSize: 10, color: textColor.withOpacity(0.6))),
            if (isOutbound) ...[
              const SizedBox(width: 4),
              _StatusIcon(status: message.status, color: textColor.withOpacity(0.6)),
            ],
          ]),
        ]),
      ),
    );
  }
}

class _MediaPlaceholder extends StatelessWidget {
  final IconData icon; final String label; final Color color;
  const _MediaPlaceholder({required this.icon, required this.label, required this.color});
  @override
  Widget build(BuildContext context) {
    return Row(mainAxisSize: MainAxisSize.min, children: [
      Icon(icon, color: color.withOpacity(0.7), size: 20),
      const SizedBox(width: 8),
      Text(label, style: TextStyle(color: color.withOpacity(0.7), fontSize: 14)),
    ]);
  }
}

class _StatusIcon extends StatelessWidget {
  final String? status; final Color color;
  const _StatusIcon({required this.status, required this.color});
  @override
  Widget build(BuildContext context) {
    final icon = switch (status) {
      'pending' => Icons.access_time,
      'sent' => Icons.check,
      'delivered' => Icons.done_all,
      'read' => Icons.done_all,
      'failed' => Icons.error_outline,
      _ => Icons.access_time,
    };
    final colorOverride = status == 'read' ? Colors.lightBlue : status == 'failed' ? AppColors.error : null;
    return Icon(icon, size: 14, color: colorOverride ?? color);
  }
}
