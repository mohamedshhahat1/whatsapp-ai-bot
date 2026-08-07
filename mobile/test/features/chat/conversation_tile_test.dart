import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:whatsapp_ai_mobile/features/chat/chat_models.dart';
import 'package:whatsapp_ai_mobile/features/chat/widgets/channel_badge.dart';
import 'package:whatsapp_ai_mobile/features/chat/widgets/conversation_tile.dart';

final _now = DateTime.now().toUtc().toIso8601String();

Conversation _conversation({
  int id = 1,
  int userId = 42,
  String channel = channelWhatsapp,
  String status = statusActive,
  String mode = modeBot,
  String? assignedOperator,
  String? tag,
  String? sessionState,
}) =>
    Conversation(
      id: id,
      userId: userId,
      channel: channel,
      status: status,
      mode: mode,
      tag: tag,
      assignedOperator: assignedOperator,
      sessionState: sessionState,
      createdAt: _now,
      updatedAt: _now,
    );

Future<void> _pumpTile(WidgetTester tester, Conversation conversation, {bool isUnread = false, bool isLead = false}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: ConversationTile(conversation: conversation, isUnread: isUnread, isLead: isLead, onTap: () {}),
      ),
    ),
  );
  // pump rather than pumpAndSettle: the lead star and the unread dot are
  // animated, and settling buys nothing this test asserts on.
  await tester.pump();
}

void main() {
  group('channel badge', () {
    // WhatsApp is the only enabled channel on a stock deployment, so badging
    // it would put an identical pill on every row and buy nothing.
    testWidgets('is suppressed for WhatsApp', (tester) async {
      await _pumpTile(tester, _conversation());

      expect(find.byType(ChannelBadge), findsNothing);
      expect(find.text('WhatsApp'), findsNothing);
    });

    testWidgets('is shown for Messenger', (tester) async {
      await _pumpTile(tester, _conversation(channel: channelMessenger));

      expect(find.byType(ChannelBadge), findsOneWidget);
      expect(find.text('Messenger'), findsOneWidget);
    });

    testWidgets('is shown for every non-WhatsApp channel', (tester) async {
      for (final channel in allChannels.where((c) => c != channelWhatsapp)) {
        await _pumpTile(tester, _conversation(channel: channel));
        expect(find.byType(ChannelBadge), findsOneWidget, reason: '$channel should be badged');
      }
    });

    testWidgets('carries the channel through to the badge', (tester) async {
      await _pumpTile(tester, _conversation(channel: channelInstagramDm));

      expect(tester.widget<ChannelBadge>(find.byType(ChannelBadge)).channel, channelInstagramDm);
    });
  });

  group('title fallback', () {
    testWidgets('uses the assigned operator when there is one', (tester) async {
      await _pumpTile(tester, _conversation(assignedOperator: 'Mohamed'));

      expect(find.text('Mohamed'), findsOneWidget);
      expect(find.text('Customer #42'), findsNothing);
    });

    testWidgets('falls back to the user id when nobody has claimed it', (tester) async {
      await _pumpTile(tester, _conversation(userId: 42));

      expect(find.text('Customer #42'), findsOneWidget);
    });
  });

  group('state label', () {
    // Returns null for the ordinary case so the common tile stays uncluttered.
    testWidgets('is absent on a plainly active session', (tester) async {
      await _pumpTile(tester, _conversation(sessionState: sessionActiveBot));

      expect(find.text('Closed'), findsNothing);
      expect(find.text('Quiet'), findsNothing);
      expect(find.text('Closing'), findsNothing);
    });

    testWidgets('reads Closed for a closed session', (tester) async {
      await _pumpTile(tester, _conversation(status: statusClosed));

      expect(find.text('Closed'), findsOneWidget);
    });

    testWidgets('reads Quiet while waiting to be swept', (tester) async {
      await _pumpTile(tester, _conversation(sessionState: sessionWaitingIdle));

      expect(find.text('Quiet'), findsOneWidget);
    });

    // Closed wins over the computed state: a swept session reports
    // sessionClosed, and showing both labels would be noise.
    testWidgets('prefers Closed over the computed state', (tester) async {
      await _pumpTile(tester, _conversation(status: statusClosed, sessionState: sessionClosed));

      expect(find.text('Closed'), findsOneWidget);
    });
  });

  group('mode badge', () {
    testWidgets('reads Bot in bot mode', (tester) async {
      await _pumpTile(tester, _conversation());

      expect(find.text('Bot'), findsOneWidget);
    });

    testWidgets('reads Human once an operator has taken over', (tester) async {
      await _pumpTile(tester, _conversation(mode: modeHuman));

      expect(find.text('Human'), findsOneWidget);
    });
  });

  testWidgets('a Messenger tile shows the channel and the mode together', (tester) async {
    await _pumpTile(tester, _conversation(channel: channelMessenger, mode: modeHuman, assignedOperator: 'Sara'));

    expect(find.text('Messenger'), findsOneWidget);
    expect(find.text('Human'), findsOneWidget);
    expect(find.text('Sara'), findsOneWidget);
  });

  testWidgets('tapping the row reports the tap', (tester) async {
    var taps = 0;
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ConversationTile(conversation: _conversation(), isUnread: false, isLead: false, onTap: () => taps++),
        ),
      ),
    );
    await tester.pump();

    await tester.tap(find.byType(ConversationTile));
    expect(taps, 1);
  });
}
