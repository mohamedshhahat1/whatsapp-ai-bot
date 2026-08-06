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
    /// Which app the customer wrote from: [channelWhatsapp],
    /// [channelMessenger] and so on. Defaulted for the reason above -- a
    /// backend that predates channels sends no such field, and every
    /// conversation from that era was WhatsApp.
    @Default(channelWhatsapp) String channel,
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
    @Default(channelWhatsapp) String channel,
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
///
/// Deliberately carries no channel either. Every summary here belongs to the
/// same customer as the conversation that opened the sheet, and a customer
/// cannot change channel -- identity is (channel, external id) -- so the field
/// would be one value repeated down the list. [CustomerHistory] holds it once.
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
///
/// Identity arrives three ways now that the platform is not WhatsApp-only.
/// [waId] is a phone number OR AN EMPTY STRING -- the backend sends `""` for
/// anyone who did not arrive on WhatsApp, and deliberately does not put a
/// page-scoped id there, because anything rendering that field as a phone
/// number would render a Messenger id as one. Use [displayId], which falls
/// back to [externalId].
@freezed
class CustomerHistory with _$CustomerHistory {
  const factory CustomerHistory({
    required int userId,
    /// Empty for every non-WhatsApp customer. Not the field to display.
    @Default('') String waId,
    @Default(channelWhatsapp) String channel,
    /// Their id on [channel]: the phone number on WhatsApp, a page-scoped id
    /// on Messenger. The only identity field populated for every channel.
    String? externalId,
    String? name,
    required int totalConversations,
    @Default(<ConversationSummary>[]) List<ConversationSummary> previous,
  }) = _CustomerHistory;
  factory CustomerHistory.fromJson(Map<String, dynamic> json) => _$CustomerHistoryFromJson(json);
}

/// Something to call this customer, whatever channel they arrived on.
///
/// The history sheet used to title itself `name ?? waId`. For a Messenger
/// customer the backend sends `wa_id: ""`, and Meta supplies no profile name
/// unless the page has requested that permission -- so both halves were empty
/// and the sheet opened with a blank heading above a list of sessions.
///
/// Order matters: a real name first, then the id they are actually reachable
/// by, then the phone number, and only then a synthetic label. The last case
/// is not hypothetical -- it is what an older backend, which sends neither
/// `channel` nor `external_id`, will produce for a customer with no name.
extension CustomerHistoryDisplay on CustomerHistory {
  String get displayId {
    if (externalId != null && externalId!.isNotEmpty) return externalId!;
    if (waId.isNotEmpty) return waId;
    return 'Customer #$userId';
  }

  String get displayName {
    final trimmed = name?.trim() ?? '';
    return trimmed.isNotEmpty ? trimmed : displayId;
  }
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

/// Channels, mirroring app/channels/constants.py. That module is append-only
/// and these strings are stored in the database, so they are a contract:
/// renaming one here would silently stop matching rows that already exist.
const channelWhatsapp = 'whatsapp';
const channelMessenger = 'messenger';
const channelInstagramDm = 'instagram_dm';
const channelFacebookComment = 'facebook_comment';
const channelInstagramComment = 'instagram_comment';

/// Every channel the backend can report, in the order they are offered in the
/// filter menu: private threads first, then the public comment channels.
const allChannels = <String>[
  channelWhatsapp,
  channelMessenger,
  channelInstagramDm,
  channelFacebookComment,
  channelInstagramComment,
];

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
