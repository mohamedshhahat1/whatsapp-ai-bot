import 'dart:async';

import 'package:whatsapp_ai_mobile/core/storage/secure_storage.dart';
import 'package:whatsapp_ai_mobile/core/websocket/websocket_service.dart';
import 'package:whatsapp_ai_mobile/features/chat/chat_models.dart';
import 'package:whatsapp_ai_mobile/features/chat/chat_repository.dart';

/// Hand-written fakes rather than a mocking package.
///
/// The app has no mocking dependency, and adding one to assert a handful of
/// call arguments would be a poor trade. These implement the concrete
/// collaborators through their implicit interfaces; the private fields on
/// those classes are not part of the interface across library boundaries, so
/// `implements` is sufficient.
class FakeChatRepository implements ChatRepository {
  FakeChatRepository({
    List<Conversation>? conversations,
    this.detail,
    this.history,
  }) : conversations = conversations ?? <Conversation>[];

  List<Conversation> conversations;
  ConversationDetail? detail;
  CustomerHistory? history;

  int listCalls = 0;
  int historyCalls = 0;
  String? lastStatusFilter;

  @override
  Future<List<Conversation>> listConversations({int offset = 0, int limit = 50, String? status}) async {
    listCalls++;
    lastStatusFilter = status;
    return conversations;
  }

  @override
  Future<ConversationDetail> getConversation(int id) async {
    final value = detail;
    if (value == null) throw StateError('no detail configured on the fake');
    return value;
  }

  @override
  Future<CustomerHistory> getHistory(int id, {int limit = 20}) async {
    historyCalls++;
    final value = history;
    if (value == null) throw StateError('no history configured on the fake');
    return value;
  }

  @override
  Future<void> deleteConversation(int id) async {}

  @override
  Future<Conversation> takeOver(int id, {String? operator}) => throw UnimplementedError();

  @override
  Future<Conversation> resumeAi(int id) => throw UnimplementedError();

  @override
  Future<ManualReply> sendReply(int id, String text) => throw UnimplementedError();
}

/// A WebSocket that never connects but hands out a real broadcast stream, so
/// ChatListNotifier's constructor subscription behaves normally and a test
/// can push events through it.
class FakeWebSocketService implements WebSocketService {
  final StreamController<WsEvent> _events = StreamController<WsEvent>.broadcast();

  void emit(WsEvent event) => _events.add(event);

  @override
  Stream<WsEvent> get eventStream => _events.stream;

  @override
  Stream<WsConnectionState> get stateStream => const Stream<WsConnectionState>.empty();

  @override
  WsConnectionState get state => WsConnectionState.disconnected;

  @override
  Future<void> connect(String baseUrl) async {}

  @override
  Future<void> disconnect() async {}

  @override
  void dispose() {
    _events.close();
  }
}

class FakeSecureStorage implements SecureStorage {
  FakeSecureStorage({this.operatorName});

  String? operatorName;
  final Map<String, String> values = <String, String>{};

  @override
  Future<void> saveApiKey(String key) async => values['apiKey'] = key;

  @override
  Future<String?> getApiKey() async => values['apiKey'];

  @override
  Future<bool> hasApiKey() async => (values['apiKey'] ?? '').isNotEmpty;

  @override
  Future<void> clearApiKey() async => values.remove('apiKey');

  @override
  Future<void> saveBaseUrl(String url) async => values['baseUrl'] = url;

  @override
  Future<String?> getBaseUrl() async => values['baseUrl'];

  @override
  Future<void> saveOperatorName(String name) async => operatorName = name;

  @override
  Future<String?> getOperatorName() async => operatorName;

  @override
  Future<void> saveThemeMode(String mode) async => values['themeMode'] = mode;

  @override
  Future<String?> getThemeMode() async => values['themeMode'];

  @override
  Future<void> saveLocale(String locale) async => values['locale'] = locale;

  @override
  Future<String?> getLocale() async => values['locale'];

  @override
  Future<void> clearAll() async {
    values.clear();
    operatorName = null;
  }
}
