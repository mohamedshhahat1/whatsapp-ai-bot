import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:whatsapp_ai_mobile/core/storage/secure_storage.dart';
import 'package:whatsapp_ai_mobile/core/websocket/websocket_service.dart';
import 'package:whatsapp_ai_mobile/features/chat/chat_list_provider.dart';
import 'package:whatsapp_ai_mobile/features/chat/chat_models.dart';
import 'package:whatsapp_ai_mobile/features/chat/chat_repository.dart';

import '../../support/fakes.dart';

Conversation _conversation({
  required int id,
  String channel = channelWhatsapp,
  String mode = modeBot,
  String status = statusActive,
  String? tag,
  String updatedAt = '2026-08-07T10:00:00Z',
}) =>
    Conversation(
      id: id,
      userId: id,
      channel: channel,
      status: status,
      mode: mode,
      tag: tag,
      createdAt: '2026-08-07T09:00:00Z',
      updatedAt: updatedAt,
    );

({ProviderContainer container, FakeChatRepository repo}) _harness(List<Conversation> conversations) {
  final repo = FakeChatRepository(conversations: conversations);
  final container = ProviderContainer(
    overrides: <Override>[
      chatRepositoryProvider.overrideWithValue(repo),
      webSocketServiceProvider.overrideWithValue(FakeWebSocketService()),
      secureStorageProvider.overrideWithValue(FakeSecureStorage()),
    ],
  );
  addTearDown(container.dispose);
  return (container: container, repo: repo);
}

void main() {
  group('ChatListState.copyWith', () {
    // The bug this sentinel fixed: `x ?? this.x` meant passing null preserved
    // the old value, so "All", "All sessions" and closing the search box were
    // all inert.
    const populated = ChatListState(
      searchQuery: 'ahmed',
      modeFilter: modeHuman,
      statusFilter: statusActive,
      channelFilter: channelMessenger,
    );

    test('omitting an argument preserves it', () {
      final copy = populated.copyWith();

      expect(copy.searchQuery, 'ahmed');
      expect(copy.modeFilter, modeHuman);
      expect(copy.statusFilter, statusActive);
      expect(copy.channelFilter, channelMessenger);
    });

    test('passing null clears the channel filter', () {
      expect(populated.copyWith(channelFilter: null).channelFilter, isNull);
    });

    test('passing null clears the mode filter', () {
      expect(populated.copyWith(modeFilter: null).modeFilter, isNull);
    });

    test('passing null clears the status filter', () {
      expect(populated.copyWith(statusFilter: null).statusFilter, isNull);
    });

    test('passing null clears the search query', () {
      expect(populated.copyWith(searchQuery: null).searchQuery, isNull);
    });

    test('clearing one filter leaves the others alone', () {
      final copy = populated.copyWith(channelFilter: null);

      expect(copy.channelFilter, isNull);
      expect(copy.modeFilter, modeHuman);
      expect(copy.statusFilter, statusActive);
      expect(copy.searchQuery, 'ahmed');
    });

    test('a new value replaces the old one', () {
      expect(populated.copyWith(channelFilter: channelWhatsapp).channelFilter, channelWhatsapp);
    });

    // Characterisation, not endorsement. errorMessage is the one field with no
    // sentinel, so every copyWith drops it -- which is what makes an error
    // vanish on the next state change rather than persisting.
    test('errorMessage is dropped by any copyWith, unlike the filters', () {
      const withError = ChatListState(errorMessage: 'boom');

      expect(withError.copyWith(hasMore: false).errorMessage, isNull);
    });

    test('non-nullable fields still follow the ?? rule', () {
      const state = ChatListState(offset: 40, hasMore: false);

      expect(state.copyWith().offset, 40);
      expect(state.copyWith().hasMore, isFalse);
      expect(state.copyWith(offset: 0).offset, 0);
    });
  });

  group('channel filtering', () {
    final conversations = <Conversation>[
      _conversation(id: 1, updatedAt: '2026-08-07T10:00:00Z'),
      _conversation(id: 2, channel: channelMessenger, updatedAt: '2026-08-07T09:00:00Z'),
      _conversation(id: 3, channel: channelMessenger, updatedAt: '2026-08-07T08:00:00Z'),
    ];

    test('shows every conversation when no filter is set', () async {
      final harness = _harness(conversations);
      final notifier = harness.container.read(chatListProvider.notifier);
      await notifier.refresh();

      expect(notifier.filtered, hasLength(3));
    });

    test('narrows to one channel', () async {
      final harness = _harness(conversations);
      final notifier = harness.container.read(chatListProvider.notifier);
      await notifier.refresh();

      notifier.setChannelFilter(channelMessenger);

      expect(notifier.filtered.map((c) => c.id), <int>[2, 3]);
    });

    test('"All" clears the filter and brings every row back', () async {
      final harness = _harness(conversations);
      final notifier = harness.container.read(chatListProvider.notifier);
      await notifier.refresh();
      notifier.setChannelFilter(channelMessenger);
      expect(notifier.filtered, hasLength(2));

      notifier.setChannelFilter(null);

      expect(harness.container.read(chatListProvider).channelFilter, isNull);
      expect(notifier.filtered, hasLength(3));
    });

    test('a channel with no rows filters everything out rather than erroring', () async {
      final harness = _harness(conversations);
      final notifier = harness.container.read(chatListProvider.notifier);
      await notifier.refresh();

      notifier.setChannelFilter(channelInstagramDm);

      expect(notifier.filtered, isEmpty);
    });

    // No refetch, because the list endpoint has no channel parameter to
    // refetch with. Contrast with setStatusFilter below.
    test('does not refetch, because the filter is client-side', () async {
      final harness = _harness(conversations);
      final notifier = harness.container.read(chatListProvider.notifier);
      await notifier.refresh();
      final callsBefore = harness.repo.listCalls;

      notifier.setChannelFilter(channelMessenger);

      expect(harness.repo.listCalls, callsBefore);
    });

    test('the status filter does refetch, and is passed to the server', () async {
      final harness = _harness(conversations);
      final notifier = harness.container.read(chatListProvider.notifier);
      await notifier.refresh();
      final callsBefore = harness.repo.listCalls;

      notifier.setStatusFilter(statusClosed);
      await Future<void>.delayed(Duration.zero);

      expect(harness.repo.listCalls, greaterThan(callsBefore));
      expect(harness.repo.lastStatusFilter, statusClosed);
    });

    test('combines with the mode filter rather than replacing it', () async {
      final harness = _harness(<Conversation>[
        _conversation(id: 1, channel: channelMessenger),
        _conversation(id: 2, channel: channelMessenger, mode: modeHuman),
        _conversation(id: 3, mode: modeHuman),
      ]);
      final notifier = harness.container.read(chatListProvider.notifier);
      await notifier.refresh();

      notifier.setChannelFilter(channelMessenger);
      notifier.setModeFilter(modeHuman);

      expect(notifier.filtered.map((c) => c.id), <int>[2]);
    });
  });

  group('availableChannels', () {
    test('is empty before anything has loaded', () {
      final harness = _harness(<Conversation>[]);

      expect(harness.container.read(chatListProvider.notifier).availableChannels, isEmpty);
    });

    // Derived from loaded rows rather than from allChannels, so a
    // WhatsApp-only deployment gets no channel section at all instead of four
    // options that can never match.
    test('reports only the channels actually present', () async {
      final harness = _harness(<Conversation>[
        _conversation(id: 1),
        _conversation(id: 2, channel: channelMessenger),
      ]);
      final notifier = harness.container.read(chatListProvider.notifier);
      await notifier.refresh();

      expect(notifier.availableChannels, <String>[channelWhatsapp, channelMessenger]);
    });

    test('deduplicates', () async {
      final harness = _harness(<Conversation>[
        _conversation(id: 1, channel: channelMessenger),
        _conversation(id: 2, channel: channelMessenger),
      ]);
      final notifier = harness.container.read(chatListProvider.notifier);
      await notifier.refresh();

      expect(notifier.availableChannels, <String>[channelMessenger]);
    });

    test('orders by the canonical list, not by arrival', () async {
      final harness = _harness(<Conversation>[
        _conversation(id: 1, channel: channelInstagramComment),
        _conversation(id: 2, channel: channelMessenger),
        _conversation(id: 3),
      ]);
      final notifier = harness.container.read(chatListProvider.notifier);
      await notifier.refresh();

      expect(notifier.availableChannels, <String>[channelWhatsapp, channelMessenger, channelInstagramComment]);
    });

    test('sorts an unknown channel last rather than dropping it', () async {
      final harness = _harness(<Conversation>[
        _conversation(id: 1, channel: 'telegram'),
        _conversation(id: 2),
      ]);
      final notifier = harness.container.read(chatListProvider.notifier);
      await notifier.refresh();

      expect(notifier.availableChannels, <String>[channelWhatsapp, 'telegram']);
    });
  });

  group('ordering', () {
    // The lead pin applies only to ACTIVE sessions: the sales_lead tag is
    // sticky, so a closed lead from last week used to sit permanently above a
    // live customer waiting right now.
    test('pins an unclaimed active lead above a newer ordinary row', () async {
      final harness = _harness(<Conversation>[
        _conversation(id: 1, updatedAt: '2026-08-07T12:00:00Z'),
        _conversation(id: 2, tag: tagSalesLead, updatedAt: '2026-08-07T08:00:00Z'),
      ]);
      final notifier = harness.container.read(chatListProvider.notifier);
      await notifier.refresh();

      expect(notifier.filtered.first.id, 2);
    });

    test('does not pin a closed lead', () async {
      final harness = _harness(<Conversation>[
        _conversation(id: 1, updatedAt: '2026-08-07T12:00:00Z'),
        _conversation(id: 2, tag: tagSalesLead, status: statusClosed, updatedAt: '2026-08-07T08:00:00Z'),
      ]);
      final notifier = harness.container.read(chatListProvider.notifier);
      await notifier.refresh();

      expect(notifier.filtered.first.id, 1);
    });
  });
}
