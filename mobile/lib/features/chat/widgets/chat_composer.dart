import 'package:flutter/material.dart';
import 'package:flutter_animate/flutter_animate.dart';

import '../../../core/theme/app_colors.dart';

class ChatComposer extends StatefulWidget {
  final Future<bool> Function(String text) onSend;
  final bool isSending;
  const ChatComposer({super.key, required this.onSend, this.isSending = false});
  @override
  State<ChatComposer> createState() => _ChatComposerState();
}

class _ChatComposerState extends State<ChatComposer> {
  final _controller = TextEditingController();
  bool _hasText = false;
  @override
  void dispose() { _controller.dispose(); super.dispose(); }

  Future<void> _send() async {
    final text = _controller.text.trim();
    if (text.isEmpty) return;
    _controller.clear();
    setState(() => _hasText = false);
    await widget.onSend(text);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isDark = theme.brightness == Brightness.dark;
    return SafeArea(
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 8),
        decoration: BoxDecoration(color: isDark ? AppColors.darkSurface : AppColors.lightSurface, border: Border(top: BorderSide(color: theme.dividerColor, width: 0.5))),
        child: Row(children: [
          IconButton(icon: const Icon(Icons.add_circle_outline, size: 24), onPressed: () => _showAttachmentSheet(context)),
          Expanded(
            child: TextField(
              controller: _controller, maxLines: 5, minLines: 1, textInputAction: TextInputAction.send,
              onChanged: (v) => setState(() => _hasText = v.trim().isNotEmpty),
              onSubmitted: (_) => _send(),
              decoration: InputDecoration(
                hintText: 'Type a message...', isDense: true, contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                border: OutlineInputBorder(borderRadius: BorderRadius.circular(24), borderSide: BorderSide.none),
                filled: true, fillColor: isDark ? AppColors.darkBg.withOpacity(0.5) : AppColors.lightBg,
                suffixIcon: IconButton(icon: const Icon(Icons.emoji_emotions_outlined, size: 22), onPressed: () {}),
              ),
            ),
          ),
          const SizedBox(width: 4),
          if (_hasText || widget.isSending)
            CircleAvatar(radius: 22, backgroundColor: AppColors.primary, child: widget.isSending
                ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                : IconButton(icon: const Icon(Icons.send, color: Colors.white, size: 20), onPressed: _send)).animate().scale(duration: 200.ms)
          else
            CircleAvatar(radius: 22, backgroundColor: theme.colorScheme.surfaceContainerHighest, child: IconButton(icon: Icon(Icons.mic, color: theme.colorScheme.onSurfaceVariant, size: 20), onPressed: () {})),
        ]),
      ),
    );
  }

  void _showAttachmentSheet(BuildContext context) {
    showModalBottomSheet(
      context: context,
      builder: (context) => SafeArea(
        child: Padding(padding: const EdgeInsets.all(16), child: Column(mainAxisSize: MainAxisSize.min, children: [
          ListTile(leading: const Icon(Icons.photo_outlined), title: const Text('Photo'), onTap: () => Navigator.pop(context)),
          ListTile(leading: const Icon(Icons.camera_alt_outlined), title: const Text('Camera'), onTap: () => Navigator.pop(context)),
          ListTile(leading: const Icon(Icons.insert_drive_file_outlined), title: const Text('Document'), onTap: () => Navigator.pop(context)),
        ])),
      ),
    );
  }
}
