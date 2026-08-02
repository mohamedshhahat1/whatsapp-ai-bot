import 'package:freezed_annotation/freezed_annotation.dart';

part 'chat_models.freezed.dart';
part 'chat_models.g.dart';

/// One SESSION, not one customer.
///
/// Since conversations close themselves after a period of silence, a customer
/// who comes back gets a NEW conversation with a new [id] and its own
/// transcript. The two are never merged. [userId] is the only stable
/// per-customer key, so the same person legitimately appears several times in
/// the chat list -- that is history, not duplication.
///
/// Every lifecycle field below is nullable with a default. That is not
/// defensiveness for its own sake: a phone runs whatever build its owner last
/// installed, so this model will meet a backend that predates these fields.
/// Marking them required would throw inside fromJson and take the entire chat
/// list down rather than degrade one badge.
@freezed
class Conversation with _$Conversation {
  const factory Conversation({
    required int id,
    required int userId,
    /// 'active' or 'closed'.
    required String status,
    required String mode,
    String? tag,
    String? assignedOperator,
    String? handoffAt,
    /// When the idle countdown last restarted. Both directions of traffic
    /// reset it, so this is not 'last customer message'.
    String? lastActivityAt,
    /// Non-null once this session has greeted its customer. Survives a
    /// reopen, which is why nobody is greeted twice in one session.
    String? welcomeSentAt,
    /// Non-null once a worker has CLAIMED the goodbye -- not necessarily once
    /// one has been delivered.
    String? closingSentAt,
    String? closedAt,
    /// Computed server-side: ACTIVE_BOT, ACTIVE_HUMAN, WAITING_IDLE, CLOSING
    /// or CLOSED. Never stored, so it cannot drift from the columns it is
    /// derived from.
    String? sessionState,
    /// The server's configured idle timeout, so a countdown shown here cannot
    /// drift from CONVERSATION_IDLE_TIMEOUT_MINUTES.
    int? idleTimeoutMinutes,
    /// False when CONVERSATION_CLOSE_AFTER_IDLE is off, in which case
    /// WAITING_IDLE is a resting state rather than a countdown and the UI must
    /// not promise the session is about to end.
    bool? closeAfterIdle,
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

/// A single session and its transcript.
///
/// [messages] holds this session's messages only. Earlier visits by the same
/// customer are separate conversations with their own ids; fetch
/// [CustomerHistory] to find them.
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
    String? lastActivityAt,
    String? welcomeSentAt,
    String? closingSentAt,
    String? closedAt,
    String? sessionState,
    int? idleTimeoutMinutes,
    bool? closeAfterIdle,
    required String createdAt,
    required String updatedAt,
    required List<Message> messages,
  }) = _ConversationDetail;
  factory ConversationDetail.fromJson(Map<String, dynamic> json) => _$ConversationDetailFromJson(json);
}

/// One of a customer's other visits. Deliberately has no transcript: the
/// history sheet lists several and loading every message of each would be a
/// large payload for a panel nobody has opened yet.
@freezed
class ConversationSummary with _$ConversationSummary {
  const factory ConversationSummary({
    required int id,
    required String status,
    required String mode,
    String? tag,
    required String createdAt,
    required String updatedAt,
    String? closedAt,
  }) = _ConversationSummary;
  factory ConversationSummary.fromJson(Map<String, dynamic> json) => _$ConversationSummaryFromJson(json);
}

/// The customer behind a conversation, and their earlier sessions.
///
/// Sessions are not merged -- the gaps between them are the point of the
/// lifecycle -- so this is navigation between them plus the count, which is
/// the part that changes how an operator talks to someone.
///
/// Operator-facing only. None of it is fed to the model, which still sees the
/// current session alone.
@freezed
class CustomerHistory with _$CustomerHistory {
  const factory CustomerHistory({
    required int userId,
    required String waId,
    String? name,
    required int totalConversations,
    @Default(<ConversationSummary>[]) List<ConversationSummary> previous,
  }) = _CustomerHistory;
  factory CustomerHistory.fromJson(Map<String, dynamic> json) => _$CustomerHistoryFromJson(json);
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

/// The other half of [statusActive], which was missing -- there was no
/// constant for a closed session, so no check against one could be written.
const statusClosed = 'closed';

/// Computed session states, as returned in [Conversation.sessionState].
const sessionActiveBot = 'ACTIVE_BOT';
const sessionActiveHuman = 'ACTIVE_HUMAN';
const sessionWaitingIdle = 'WAITING_IDLE';
const sessionClosing = 'CLOSING';
const sessionClosed = 'CLOSED';

/// Returned as the error code in a 409 when an operator acts on a session
/// whose customer has already started a newer one. The remedy is specific --
/// open the newer session -- so it is worth telling apart from other
/// conflicts.
const codeSuperseded = 'conversation_superseded';

const dirInbound = 'inbound';
const dirOutbound = 'outbound';
