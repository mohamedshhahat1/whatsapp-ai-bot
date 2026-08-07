import 'package:flutter_test/flutter_test.dart';
import 'package:whatsapp_ai_mobile/features/chat/chat_models.dart';

/// A conversation exactly as app/schemas/conversation.py serialises it.
///
/// snake_case throughout, because that is what the backend sends. If these
/// keys ever have to be camelCased to make the test pass, the app has
/// stopped being able to read its own API.
Map<String, dynamic> _conversationJson({String? channel = channelMessenger}) => <String, dynamic>{
      'id': 7,
      'user_id': 42,
      if (channel != null) 'channel': channel,
      'status': 'active',
      'mode': 'bot',
      'tag': null,
      'assigned_operator': null,
      'handoff_at': null,
      'last_activity_at': '2026-08-07T10:00:00Z',
      'welcome_sent_at': '2026-08-07T09:00:00Z',
      'closing_sent_at': null,
      'closed_at': null,
      'session_state': 'ACTIVE_BOT',
      'idle_timeout_minutes': 5,
      'close_after_idle': true,
      'created_at': '2026-08-07T09:00:00Z',
      'updated_at': '2026-08-07T10:00:00Z',
    };

void main() {
  group('Conversation.fromJson', () {
    test('reads the channel the backend sent', () {
      final conversation = Conversation.fromJson(_conversationJson());

      expect(conversation.channel, channelMessenger);
      expect(conversation.id, 7);
      expect(conversation.userId, 42);
    });

    // The compatibility promise in the model's own doc comment: a phone runs
    // whatever build its owner last installed, so this model will meet a
    // backend that predates the channel field.
    test('defaults the channel to WhatsApp when the field is absent', () {
      final conversation = Conversation.fromJson(_conversationJson(channel: null));

      expect(conversation.channel, channelWhatsapp);
    });

    test('survives a payload with none of the lifecycle fields', () {
      final conversation = Conversation.fromJson(<String, dynamic>{
        'id': 1,
        'user_id': 2,
        'status': 'active',
        'mode': 'bot',
        'created_at': '2026-08-07T09:00:00Z',
        'updated_at': '2026-08-07T09:00:00Z',
      });

      expect(conversation.channel, channelWhatsapp);
      expect(conversation.sessionState, isNull);
      expect(conversation.closeAfterIdle, isNull);
    });

    test('keeps an unknown channel rather than rejecting the row', () {
      final conversation = Conversation.fromJson(_conversationJson(channel: 'telegram'));

      expect(conversation.channel, 'telegram');
    });
  });

  group('CustomerHistory.fromJson', () {
    test('reads external_id and channel', () {
      final history = CustomerHistory.fromJson(<String, dynamic>{
        'user_id': 42,
        'wa_id': '',
        'channel': channelMessenger,
        'external_id': '5551234567890',
        'name': null,
        'total_conversations': 3,
        'previous': <Map<String, dynamic>>[],
      });

      expect(history.channel, channelMessenger);
      expect(history.externalId, '5551234567890');
      expect(history.waId, '');
      expect(history.totalConversations, 3);
    });

    test('defaults channel, wa_id and previous on an older payload', () {
      final history = CustomerHistory.fromJson(<String, dynamic>{
        'user_id': 42,
        'total_conversations': 1,
      });

      expect(history.channel, channelWhatsapp);
      expect(history.waId, '');
      expect(history.externalId, isNull);
      expect(history.previous, isEmpty);
    });

    test('parses previous sessions', () {
      final history = CustomerHistory.fromJson(<String, dynamic>{
        'user_id': 42,
        'total_conversations': 2,
        'previous': <Map<String, dynamic>>[
          <String, dynamic>{
            'id': 5,
            'status': 'closed',
            'mode': 'bot',
            'tag': null,
            'created_at': '2026-08-01T09:00:00Z',
            'updated_at': '2026-08-01T10:00:00Z',
            'closed_at': '2026-08-01T10:00:00Z',
          },
        ],
      });

      expect(history.previous, hasLength(1));
      expect(history.previous.single.id, 5);
      expect(history.previous.single.status, statusClosed);
    });
  });

  group('Message.fromJson', () {
    test('reads the snake_case message fields', () {
      final message = Message.fromJson(<String, dynamic>{
        'id': 11,
        'wa_message_id': 'mid.in.abc.1',
        'direction': 'inbound',
        'type': 'text',
        'content': 'hello',
        'media_id': null,
        'status': null,
        'created_at': '2026-08-07T09:00:00Z',
      });

      expect(message.waMessageId, 'mid.in.abc.1');
      expect(message.direction, dirInbound);
      expect(message.content, 'hello');
    });
  });

  // This is the blank-title bug. A Messenger customer has wa_id "" by design,
  // and Meta supplies no profile name unless the page has asked for that
  // permission -- so the old `name ?? waId` title rendered as nothing at all
  // above a list of sessions.
  group('CustomerHistory display fallbacks', () {
    const messengerNoName = CustomerHistory(
      userId: 42,
      channel: channelMessenger,
      externalId: '5551234567890',
      totalConversations: 2,
    );

    test('the heading is never blank for an unnamed Messenger customer', () {
      expect(messengerNoName.displayName, isNotEmpty);
      expect(messengerNoName.displayName, '5551234567890');
    });

    test('prefers a real name over any id', () {
      const named = CustomerHistory(
        userId: 42,
        waId: '201234567890',
        externalId: '5551234567890',
        name: 'Mohamed',
        totalConversations: 2,
      );

      expect(named.displayName, 'Mohamed');
    });

    test('treats a whitespace-only name as no name', () {
      const blank = CustomerHistory(
        userId: 42,
        waId: '201234567890',
        name: '   ',
        totalConversations: 1,
      );

      expect(blank.displayName, '201234567890');
    });

    test('trims a padded name rather than rendering the padding', () {
      const padded = CustomerHistory(userId: 42, name: '  Mohamed  ', totalConversations: 1);

      expect(padded.displayName, 'Mohamed');
    });

    // Order matters: external_id, then wa_id, then a synthetic label.
    test('displayId prefers external_id', () {
      const both = CustomerHistory(
        userId: 42,
        waId: '201234567890',
        externalId: '5551234567890',
        totalConversations: 1,
      );

      expect(both.displayId, '5551234567890');
    });

    test('displayId falls back to wa_id when external_id is null', () {
      const waOnly = CustomerHistory(userId: 42, waId: '201234567890', totalConversations: 1);

      expect(waOnly.displayId, '201234567890');
    });

    test('displayId falls back to wa_id when external_id is empty', () {
      const emptyExternal = CustomerHistory(
        userId: 42,
        waId: '201234567890',
        externalId: '',
        totalConversations: 1,
      );

      expect(emptyExternal.displayId, '201234567890');
    });

    // Not hypothetical: this is what an older backend, which sends neither
    // channel nor external_id, produces for a customer with no name.
    test('displayId falls back to a synthetic label when it has nothing else', () {
      const nothing = CustomerHistory(userId: 42, totalConversations: 1);

      expect(nothing.displayId, 'Customer #42');
      expect(nothing.displayName, 'Customer #42');
    });

    test('the full fallback order holds', () {
      const withName = CustomerHistory(userId: 1, waId: 'w', externalId: 'e', name: 'n', totalConversations: 1);
      const withExternal = CustomerHistory(userId: 1, waId: 'w', externalId: 'e', totalConversations: 1);
      const withWaId = CustomerHistory(userId: 1, waId: 'w', totalConversations: 1);
      const withNeither = CustomerHistory(userId: 1, totalConversations: 1);

      expect(withName.displayName, 'n');
      expect(withExternal.displayName, 'e');
      expect(withWaId.displayName, 'w');
      expect(withNeither.displayName, 'Customer #1');
    });
  });

  group('channel constants', () {
    // These strings are stored in the database and mirror
    // app/channels/constants.py, so renaming one would silently stop matching
    // rows that already exist.
    test('match the backend spelling exactly', () {
      expect(channelWhatsapp, 'whatsapp');
      expect(channelMessenger, 'messenger');
      expect(channelInstagramDm, 'instagram_dm');
      expect(channelFacebookComment, 'facebook_comment');
      expect(channelInstagramComment, 'instagram_comment');
    });

    test('allChannels lists every channel once, private threads first', () {
      expect(allChannels, <String>[
        channelWhatsapp,
        channelMessenger,
        channelInstagramDm,
        channelFacebookComment,
        channelInstagramComment,
      ]);
      expect(allChannels.toSet(), hasLength(allChannels.length));
    });
  });
}
