import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/error/failures.dart';
import '../../core/network/dio_client.dart';
import '../../core/network/api_endpoints.dart';
import 'chat_models.dart';

/// Thrown when an operator acts on a session whose customer has already
/// started a newer one.
///
/// Kept distinct from the generic failures because the remedy is specific and
/// permanent: this conversation can never be written to again, and the
/// operator should be sent to the customer's current session rather than
/// invited to retry. Collapsing it into UnknownFailure would produce a retry
/// button that can only ever fail.
class ConversationSuperseded implements Exception {
  const ConversationSuperseded(this.message);
  final String message;

  @override
  String toString() => message;
}

/// Recognises the superseded 409 from the raw response.
///
/// This has to run before the `e.error` mapping in each catch block, because
/// that mapping flattens every DioException into a Failure and the status code
/// is gone by then.
Exception? _supersededOrNull(DioException e) {
  final response = e.response;
  if (response?.statusCode != 409) return null;
  final data = response?.data;
  final detail = data is Map ? data['detail'] : null;
  final code = data is Map ? data['code'] : null;
  final text = (detail ?? code ?? '').toString();
  if (code == codeSuperseded || text.contains(codeSuperseded)) {
    return ConversationSuperseded(
      text.isEmpty
          ? 'This customer has already started a newer conversation.'
          : text,
    );
  }
  // Some other conflict -- most likely the 24-hour WhatsApp service window,
  // which is temporary and worth reporting as an ordinary failure.
  return null;
}

class ChatRepository {
  ChatRepository(this._dio);
  final DioClient _dio;

  Future<List<Conversation>> listConversations({
    int offset = 0,
    int limit = 50,
    String? status,
  }) async {
    try {
      final res = await _dio.get(
        ApiEndpoints.conversations(offset: offset, limit: limit, status: status),
      );
      final list = res.data as List;
      return list.map((e) => Conversation.fromJson(e as Map<String, dynamic>)).toList();
    } on DioException catch (e) { throw e.error ?? const UnknownFailure(); }
  }

  Future<ConversationDetail> getConversation(int id) async {
    try {
      final res = await _dio.get(ApiEndpoints.conversation(id));
      return ConversationDetail.fromJson(res.data as Map<String, dynamic>);
    } on DioException catch (e) { throw e.error ?? const UnknownFailure(); }
  }

  /// The customer behind a session, and their earlier sessions.
  ///
  /// Operator-facing only: none of this is fed to the model, which still sees
  /// the current session alone.
  Future<CustomerHistory> getHistory(int id, {int limit = 20}) async {
    try {
      final res = await _dio.get(ApiEndpoints.conversationHistory(id, limit: limit));
      return CustomerHistory.fromJson(res.data as Map<String, dynamic>);
    } on DioException catch (e) { throw e.error ?? const UnknownFailure(); }
  }

  Future<void> deleteConversation(int id) async {
    try { await _dio.delete(ApiEndpoints.conversation(id)); }
    on DioException catch (e) { throw e.error ?? const UnknownFailure(); }
  }

  /// Takes the conversation over. Reopens it first if the session has closed,
  /// so the operator is never handed a conversation the customer cannot reply
  /// into.
  Future<Conversation> takeOver(int id, {String? operator}) async {
    try {
      final res = await _dio.post(ApiEndpoints.takeOver(id), data: {'operator': operator});
      return Conversation.fromJson(res.data as Map<String, dynamic>);
    } on DioException catch (e) {
      throw _supersededOrNull(e) ?? e.error ?? const UnknownFailure();
    }
  }

  /// Hands the conversation back to the bot. Also reopens a closed session,
  /// and resets the idle timer so it does not close again immediately.
  Future<Conversation> resumeAi(int id) async {
    try {
      final res = await _dio.post(ApiEndpoints.resumeAi(id));
      return Conversation.fromJson(res.data as Map<String, dynamic>);
    } on DioException catch (e) {
      throw _supersededOrNull(e) ?? e.error ?? const UnknownFailure();
    }
  }

  /// Sends a manual reply. A closed session is reopened first, keeping the
  /// reply and the customer's answer in the same conversation.
  Future<ManualReply> sendReply(int id, String text) async {
    try {
      final res = await _dio.post(ApiEndpoints.reply(id), data: {'text': text});
      return ManualReply.fromJson(res.data as Map<String, dynamic>);
    } on DioException catch (e) {
      throw _supersededOrNull(e) ?? e.error ?? const UnknownFailure();
    }
  }
}

final chatRepositoryProvider = Provider<ChatRepository>((ref) {
  return ChatRepository(ref.watch(dioClientProvider));
});
