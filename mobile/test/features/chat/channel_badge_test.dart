import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:whatsapp_ai_mobile/features/chat/chat_models.dart';
import 'package:whatsapp_ai_mobile/features/chat/widgets/channel_badge.dart';

/// The badge is the only thing distinguishing a phone number from a
/// page-scoped id in the UI, so getting the wrong one on screen is not a
/// cosmetic failure -- it tells an operator the customer is reachable
/// somewhere they are not.
Widget _host(Widget child) => MaterialApp(home: Scaffold(body: Center(child: child)));

Icon _iconOf(WidgetTester tester) => tester.widget<Icon>(find.byType(Icon));

void main() {
  group('ChannelDisplay.of', () {
    test('resolves every channel the backend can report', () {
      expect(ChannelDisplay.of(channelWhatsapp).label, 'WhatsApp');
      expect(ChannelDisplay.of(channelMessenger).label, 'Messenger');
      expect(ChannelDisplay.of(channelInstagramDm).label, 'Instagram');
      expect(ChannelDisplay.of(channelFacebookComment).label, 'FB comment');
      expect(ChannelDisplay.of(channelInstagramComment).label, 'IG comment');
    });

    // Grouped rather than counted so a failure names the offenders. Several
    // Material icon names are aliases for one codepoint, and knowing that two
    // of five collide is not enough to know which two.
    test('gives each channel its own icon', () {
      final byIcon = <IconData, List<String>>{};
      for (final channel in allChannels) {
        byIcon.putIfAbsent(ChannelDisplay.of(channel).icon, () => <String>[]).add(channel);
      }

      final shared = <String>[];
      for (final entry in byIcon.entries) {
        if (entry.value.length > 1) {
          shared.add('${entry.value.join(' + ')} all draw 0x${entry.key.codePoint.toRadixString(16)}');
        }
      }

      expect(shared, isEmpty, reason: 'showLabel:false leaves the icon as the only cue, so channels sharing a glyph are indistinguishable');
    });

    test('gives each channel its own colour', () {
      final colours = allChannels.map((c) => ChannelDisplay.of(c).color).toSet();
      expect(colours.length, allChannels.length);
    });

    // A newer backend talking to an older build is the normal state of an
    // installed app, so this path is reached in production, not just in
    // theory.
    test('falls back to the raw name for a channel it has never heard of', () {
      final display = ChannelDisplay.of('telegram');
      expect(display.label, 'telegram');
      expect(display.icon, Icons.forum_outlined);
    });

    // The fallback glyph has to stay clear of the five real ones too,
    // otherwise an unrecognised channel impersonates a known one.
    test('does not reuse a real channel glyph for the fallback', () {
      final known = allChannels.map((c) => ChannelDisplay.of(c).icon).toSet();
      expect(known.contains(ChannelDisplay.of('telegram').icon), isFalse);
    });

    test('never throws on an unknown channel', () {
      expect(() => ChannelDisplay.of(''), returnsNormally);
      expect(() => ChannelDisplay.of('a brand new channel'), returnsNormally);
    });
  });

  group('ChannelBadge', () {
    // Not chat_bubble_outline: that name is an alias for messenger_outline,
    // so asserting it here would pass while the two channels drew the same
    // glyph. See the note on ChannelDisplay.
    testWidgets('draws the WhatsApp icon and label', (tester) async {
      await tester.pumpWidget(_host(const ChannelBadge(channel: channelWhatsapp)));

      expect(find.text('WhatsApp'), findsOneWidget);
      expect(_iconOf(tester).icon, Icons.perm_phone_msg_outlined);
    });

    testWidgets('draws the Messenger icon and label', (tester) async {
      await tester.pumpWidget(_host(const ChannelBadge(channel: channelMessenger)));

      expect(find.text('Messenger'), findsOneWidget);
      expect(_iconOf(tester).icon, Icons.messenger_outline);
    });

    testWidgets('colours the icon with the platform colour', (tester) async {
      await tester.pumpWidget(_host(const ChannelBadge(channel: channelMessenger)));

      expect(_iconOf(tester).color, const Color(0xFF0084FF));
    });

    // showLabel: false exists for narrow rows, where the label is what pushes
    // the timestamp off the end.
    testWidgets('drops the label but keeps the icon when showLabel is false', (tester) async {
      await tester.pumpWidget(_host(const ChannelBadge(channel: channelMessenger, showLabel: false)));

      expect(find.text('Messenger'), findsNothing);
      expect(_iconOf(tester).icon, Icons.messenger_outline);
    });

    testWidgets('renders an unknown channel rather than failing', (tester) async {
      await tester.pumpWidget(_host(const ChannelBadge(channel: 'telegram')));

      expect(find.text('telegram'), findsOneWidget);
      expect(_iconOf(tester).icon, Icons.forum_outlined);
    });
  });
}
