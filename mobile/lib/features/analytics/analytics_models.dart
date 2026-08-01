import 'package:freezed_annotation/freezed_annotation.dart';

part 'analytics_models.freezed.dart';
part 'analytics_models.g.dart';

@freezed
class CostBreakdown with _$CostBreakdown {
  const factory CostBreakdown({
    required int promptTokens,
    required int completionTokens,
    required int totalTokens,
    required double inputCostUsd,
    required double outputCostUsd,
    required double totalCostUsd,
  }) = _CostBreakdown;
  factory CostBreakdown.fromJson(Map<String, dynamic> json) => _$CostBreakdownFromJson(json);
}

@freezed
class AnalyticsOverview with _$AnalyticsOverview {
  const factory AnalyticsOverview({
    required int periodDays,
    required String since,
    required int totalUsers,
    required int totalConversations,
    required int totalMessages,
    required int newUsers,
    required int newConversations,
    required int activeConversations,
    required int messagesInPeriod,
    required int aiRequests,
    required int aiErrors,
    required double errorRate,
    required double avgLatencyMs,
    required double p95LatencyMs,
    required CostBreakdown cost,
    required double costPerConversationUsd,
    required double projectedMonthlyCostUsd,
  }) = _AnalyticsOverview;
  factory AnalyticsOverview.fromJson(Map<String, dynamic> json) => _$AnalyticsOverviewFromJson(json);
}

@freezed
class DailyUsage with _$DailyUsage {
  const factory DailyUsage({
    required String day,
    required int requests,
    required int messages,
    required int promptTokens,
    required int completionTokens,
    required int totalTokens,
    required double avgLatencyMs,
    required double costUsd,
  }) = _DailyUsage;
  factory DailyUsage.fromJson(Map<String, dynamic> json) => _$DailyUsageFromJson(json);
}

@freezed
class ModelCost with _$ModelCost {
  const factory ModelCost({
    required String model,
    required int requests,
    required int promptTokens,
    required int completionTokens,
    required int totalTokens,
    required double costUsd,
  }) = _ModelCost;
  factory ModelCost.fromJson(Map<String, dynamic> json) => _$ModelCostFromJson(json);
}

@freezed
class TopQuestion with _$TopQuestion {
  const factory TopQuestion({
    required String question,
    required int count,
    required String lastAsked,
  }) = _TopQuestion;
  factory TopQuestion.fromJson(Map<String, dynamic> json) => _$TopQuestionFromJson(json);
}

@freezed
class QuotaStats with _$QuotaStats {
  const factory QuotaStats({
    required bool available,
    String? error,
    String? date,
    double? spendUsd,
    double? spendLimitUsd,
    double? spendUsedFraction,
    int? tokens,
    int? tokenLimit,
    bool? aiDisabled,
    bool? spendGuardEnabled,
    bool? customerRateLimitEnabled,
    int? blockedCustomers,
    QuotaLimits? limits,
  }) = _QuotaStats;
  factory QuotaStats.fromJson(Map<String, dynamic> json) => _$QuotaStatsFromJson(json);
}

@freezed
class QuotaLimits with _$QuotaLimits {
  const factory QuotaLimits({
    required int perMinute,
    required int perHour,
    required int perDay,
  }) = _QuotaLimits;
  factory QuotaLimits.fromJson(Map<String, dynamic> json) => _$QuotaLimitsFromJson(json);
}
