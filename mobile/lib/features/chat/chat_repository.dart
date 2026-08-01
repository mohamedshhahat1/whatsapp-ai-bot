import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/error/failures.dart';
import '../../core/network/dio_client.dart';
import '../../core/network/api_endpoints.dart';
import 'chat_models.dart';

class ChatRepository {
  ChatRepository(this._dio);
  final DioClient _dio;

  Future<List<Conversation>> listConversations({int offset = 0, int limit = 50}) async {
    try {
      final res = await _dio.get(ApiEndpoints.conversations(offset: offset, limit: limit));
      final list = res.data as List;
      return list.map((e) => Conversation.fromJson(e as Map<String, dynamic>)).toList();
    } on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }

  Future<ConversationDetail> getConversation(int id) async {
    try {
      final res = await _dio.get(ApiEndpoints.conversation(id));
      return ConversationDetail.fromJson(res.data as Map<String, dynamic>);
    } on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }

  Future<void> deleteConversation(int id) async {
    try { await _dio.delete(ApiEndpoints.conversation(id)); }
    on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }

  Future<Conversation> takeOver(int id, {String? operator}) async {
    try {
      final res = await _dio.post(ApiEndpoints.takeOver(id), data: {'operator': operator});
      return Conversation.fromJson(res.data as Map<String, dynamic>);
    } on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }

  Future<Conversation> resumeAi(int id) async {
    try {
      final res = await _dio.post(ApiEndpoints.resumeAi(id));
      return Conversation.fromJson(res.data as Map<String, dynamic>);
    } on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }

  Future<ManualReply> sendReply(int id, String text) async {
    try {
      final res = await _dio.post(ApiEndpoints.reply(id), data: {'text': text});
      return ManualReply.fromJson(res.data as Map<String, dynamic>);
    } on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }
}

final chatRepositoryProvider = Provider<ChatRepository>((ref) {
  return ChatRepository(ref.watch(dioClientProvider));
});
