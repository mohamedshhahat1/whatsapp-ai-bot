import 'package:flutter/material.dart';

import '../chat_models.dart';

/// How each channel is drawn, in one place.
///
/// The tile, the detail screen and the filter menu all need an icon and a
/// label for the same five values. Deriving them separately is how Messenger
/// ends up blue in one view and grey in another, so they resolve here.
///
/// The colours are the platforms' own, because that is what makes a channel
/// recognisable at a glance in a list -- the same reason the bot/human badges
/// use fixed colours rather than the theme's. They are defined here rather
/// than in AppColors: AppColors describes this product's palette, and these
/// are quotations from someone else's.
///
/// Every channel needs its own glyph as well as its own colour, and that is
/// load-bearing rather than tidy: [ChannelBadge] is drawn with
/// `showLabel: false` on narrow rows, where the icon is the only thing left
/// to tell one channel from another. Several Material names are aliases for
/// a single codepoint, so picking distinct-sounding names proves nothing --
/// `chat_bubble_outline` and `messenger_outline` both resolve to 0xe155,
/// which is why WhatsApp draws a handset in a bubble rather than the plain
/// bubble you would otherwise reach for. Distinctness is asserted in
/// channel_badge_test.dart rather than assumed from the names.
class ChannelDisplay {
  const ChannelDisplay({
    required this.label,
    required this.icon,
    required this.color,
  });

  final String label;
  final IconData icon;
  final Color color;

  static const _whatsapp = ChannelDisplay(
    label: 'WhatsApp',
    icon: Icons.perm_phone_msg_outlined,
    color: Color(0xFF25D366),
  );
  static const _messenger = ChannelDisplay(
    label: 'Messenger',
    icon: Icons.messenger_outline,
    color: Color(0xFF0084FF),
  );
  static const _instagramDm = ChannelDisplay(
    label: 'Instagram',
    icon: Icons.camera_alt_outlined,
    color: Color(0xFFC13584),
  );
  static const _facebookComment = ChannelDisplay(
    label: 'FB comment',
    icon: Icons.comment_outlined,
    color: Color(0xFF1877F2),
  );
  static const _instagramComment = ChannelDisplay(
    label: 'IG comment',
    icon: Icons.rate_review_outlined,
    color: Color(0xFF8A3AB9),
  );

  /// Never throws and never returns null.
  ///
  /// An unknown channel is a newer backend talking to an older build, which
  /// is a normal state of affairs for an installed app. It gets a neutral
  /// chat icon and its raw name, which is more useful to an operator than
  /// either a crash or a silently hidden row.
  static ChannelDisplay of(String channel) {
    switch (channel) {
      case channelWhatsapp:
        return _whatsapp;
      case channelMessenger:
        return _messenger;
      case channelInstagramDm:
        return _instagramDm;
      case channelFacebookComment:
        return _facebookComment;
      case channelInstagramComment:
        return _instagramComment;
      default:
        return ChannelDisplay(
          label: channel,
          icon: Icons.forum_outlined,
          color: const Color(0xFF9E9E9E),
        );
    }
  }
}

/// The small pill shown on a conversation row.
///
/// Sized and shaped to match the bot/human and Lead badges beside it: same
/// padding, same 6px radius, same 10px icon and text.
class ChannelBadge extends StatelessWidget {
  const ChannelBadge({super.key, required this.channel, this.showLabel = true});

  final String channel;

  /// False on narrow rows, where the icon alone still identifies the channel
  /// and the label is what pushes the timestamp off the end.
  final bool showLabel;

  @override
  Widget build(BuildContext context) {
    final display = ChannelDisplay.of(channel);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: display.color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(display.icon, size: 10, color: display.color),
          if (showLabel) ...[
            const SizedBox(width: 3),
            Text(
              display.label,
              style: TextStyle(
                fontSize: 10,
                fontWeight: FontWeight.w600,
                color: display.color,
              ),
            ),
          ],
        ],
      ),
    );
  }
}
