import 'package:freezed_annotation/freezed_annotation.dart';

part 'customer_models.freezed.dart';
part 'customer_models.g.dart';

@freezed
class CustomerActivity with _$CustomerActivity {
  const factory CustomerActivity({
    required int userId,
    required String waId,
    String? name,
    required int conversations,
    required int messages,
    String? lastActive,
  }) = _CustomerActivity;
  factory CustomerActivity.fromJson(Map<String, dynamic> json) => _$CustomerActivityFromJson(json);
}

@freezed
class UserRead with _$UserRead {
  const factory UserRead({
    required int id,
    required String waId,
    String? name,
    required String createdAt,
  }) = _UserRead;
  factory UserRead.fromJson(Map<String, dynamic> json) => _$UserReadFromJson(json);
}

@freezed
class StatsRead with _$StatsRead {
  const factory StatsRead({
    required int totalUsers,
    required int totalConversations,
    required int totalMessages,
    required int messagesLast24h,
    required int totalTokensUsed,
  }) = _StatsRead;
  factory StatsRead.fromJson(Map<String, dynamic> json) => _$StatsReadFromJson(json);
}

@freezed
class UnblockResponse with _$UnblockResponse {
  const factory UnblockResponse({
    required String waId,
    required bool wasBlocked,
  }) = _UnblockResponse;
  factory UnblockResponse.fromJson(Map<String, dynamic> json) => _$UnblockResponseFromJson(json);
}
