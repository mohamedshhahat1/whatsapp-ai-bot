import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:whatsapp_ai_mobile/core/storage/secure_storage.dart';
import 'package:whatsapp_ai_mobile/core/websocket/websocket_service.dart';
import 'package:whatsapp_ai_mobile/features/chat/chat_detail_screen.dart';
import 'package:whatsapp_ai_mobile/features/chat/chat_models.dart';
import 'package:whatsapp_ai_mobile/features/chat/chat_repository.dart';
import 'package:whatsapp_ai_mobile/features/chat/widgets/channel_badge.dart';

import '../../support/fakes.dart';

const _conversationId = 7;

ConversationDetail _detail({String channel = channelWhatsapp, String mode = modeBot}) => ConversationDetail(
      id: _conversationId,
      userId: 42,
      channel: channel,
      status: statusActive,
      mode: mode,
      createdAt: '2026-08-07T09:00:00Z',
      updatedAt: '2026-08-07T10:00:00Z',
      messages: const <Message>[],
    );

/// Drives the real ChatDetailNotifier against a fake repository, so the
/// postFrameCallback load and the sheet's own Consumer are exercised rather
/// than stubbed out.
Future<FakeChatRepository> _pumpScreen(
  WidgetTester tester, {
  required ConversationDetail detail,
  CustomerHistory? history,
}) async {
  final repo = FakeChatRepository(detail: detail, history: history);
  await tester.pumpWidget(
    ProviderScope(
      overrides: <Override>[
        chatRepositoryProvider.overrideWithValue(repo),
        webSocketServiceProvider.overrideWithValue(FakeWebSocketService()),
        secureStorageProvider.overrideWithValue(FakeSecureStorage()),
      ],
      child: const MaterialApp(home: ChatDetailScreen(conversationId: _conversationId)),
    ),
  );
  // One pump to run the postFrameCallback that starts the load, one to let
  // the fake's future complete, one to rebuild with the detail.
  await tester.pump();
  await tester.pump();
  await tester.pump();
  return repo;
}

void main() {
  group('channel in the app bar', () {
    // A badge on a WhatsApp-only deployment would sit on screen permanently
    // and say nothing.
    testWidgets('is suppressed for WhatsApp', (tester) async {
      await _pumpScreen(tester, detail: _detail());

      expect(find.byType(ChannelBadge), findsNothing);
      expect(find.text('Bot Mode'), findsOneWidget);
    });

    testWidgets('is shown for Messenger, alongside the mode', (tester) async {
      await _pumpScreen(tester, detail: _detail(channel: channelMessenger));

      expect(find.byType(ChannelBadge), findsOneWidget);
      expect(find.text('Messenger'), findsOneWidget);
      expect(find.text('Bot Mode'), findsOneWidget);
    });

    testWidgets('titles the screen with the user id when nobody has claimed it', (tester) async {
      await _pumpScreen(tester, detail: _detail());

      expect(find.text('Customer #42'), findsOneWidget);
    });
  });

  group('history sheet', () {
    Future<void> openSheet(WidgetTester tester) async {
      await tester.tap(find.byIcon(Icons.history));
      await tester.pumpAndSettle();
    }

    testWidgets('renders for a Messenger customer without a blank heading', (tester) async {
      await _pumpScreen(
        tester,
        detail: _detail(channel: channelMessenger),
        history: const CustomerHistory(
          userId: 42,
          channel: channelMessenger,
          externalId: '5551234567890',
          totalConversations: 2,
          previous: <ConversationSummary>[
            ConversationSummary(
              id: 5,
              status: statusClosed,
              mode: modeBot,
              createdAt: '2026-08-01T09:00:00Z',
              updatedAt: '2026-08-01T10:00:00Z',
            ),
          ],
        ),
      );

      await openSheet(tester);

      // The bug: `name ?? waId` rendered as nothing at all here, because
      // wa_id is empty for anyone who did not arrive on WhatsApp.
      expect(find.text('5551234567890'), findsWidgets);
      expect(find.text('2 conversations in total'), findsOneWidget);
      expect(find.text('#5'), findsOneWidget);
    });

    testWidgets('badges the channel unconditionally, unlike the app bar', (tester) async {
      await _pumpScreen(
        tester,
        detail: _detail(),
        history: const CustomerHistory(
          userId: 42,
          waId: '201234567890',
          name: 'Mohamed',
          totalConversations: 1,
        ),
      );

      await openSheet(tester);

      // Absent from the app bar for WhatsApp, present in the sheet: a bare
      // string of digits is a phone number or a page-scoped id depending
      // entirely on this badge.
      expect(find.text('WhatsApp'), findsOneWidget);
      expect(find.text('Mohamed'), findsOneWidget);
    });

    testWidgets('says so when this is the customer\u0027s first conversation', (tester) async {
      await _pumpScreen(
        tester,
        detail: _detail(),
        history: const CustomerHistory(userId: 42, waId: '201234567890', totalConversations: 1),
      );

      await openSheet(tester);

      expect(find.text('This is their first conversation.'), findsOneWidget);
      expect(find.text('1 conversation in total'), findsOneWidget);
    });

    testWidgets('shows a spinner until the history arrives', (tester) async {
      await _pumpScreen(tester, detail: _detail());

      await tester.tap(find.byIcon(Icons.history));
      await tester.pump();

      expect(find.byType(CircularProgressIndicator), findsOneWidget);
    });
  });
}
