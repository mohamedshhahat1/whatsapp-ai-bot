import 'package:freezed_annotation/freezed_annotation.dart';

part 'chat_models.freezed.dart';
part 'chat_models.g.dart';

@freezed
class Conversation with _$Conversation {
  const factory Conversation({
    required int id,
    required int userId,
    required String status,
    required String mode,
    String? tag,
    String? assignedOperator,
    String? handoffAt,
    required String createdAt,
    required String updatedAt,
  }) = _Conversation;
  factory Conversation.fromJson(Map<String, dynamic> json) => _$ConversationFromJson(json);
}

@freezed
class Message with _$Message {
  const factory Message({
    required int id,
    String? waMessageId,
    required String direction,
    required String type,
    String? content,
    String? mediaId,
    String? status,
    required String createdAt,
  }) = _Message;
  factory Message.fromJson(Map<String, dynamic> json) => _$MessageFromJson(json);
}

@freezed
class ConversationDetail with _$ConversationDetail {
  const factory ConversationDetail({
    required int id,
    required int userId,
    required String status,
    required String mode,
    String? tag,
    String? assignedOperator,
    String? handoffAt,
    required String createdAt,
    required String updatedAt,
    required List<Message> messages,
  }) = _ConversationDetail;
  factory ConversationDetail.fromJson(Map<String, dynamic> json) => _$ConversationDetailFromJson(json);
}

@freezed
class ManualReply with _$ManualReply {
  const factory ManualReply({
    required int messageId,
    required int conversationId,
    String? waMessageId,
    required String sentAt,
  }) = _ManualReply;
  factory ManualReply.fromJson(Map<String, dynamic> json) => _$ManualReplyFromJson(json);
}

const tagSalesLead = 'sales_lead';
const modeBot = 'bot';
const modeHuman = 'human';
const statusActive = 'active';
const dirInbound = 'inbound';
const dirOutbound = 'outbound';
