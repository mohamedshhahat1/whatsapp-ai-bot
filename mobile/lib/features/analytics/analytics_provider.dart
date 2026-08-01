import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/error/failures.dart';
import 'analytics_models.dart';
import 'analytics_repository.dart';

class AnalyticsState {
  final AnalyticsOverview? overview;
  final List<DailyUsage> daily;
  final List<ModelCost> modelCosts;
  final List<TopQuestion> topQuestions;
  final QuotaStats? quota;
  final bool isLoading;
  final String? errorMessage;
  final int selectedDays;

  const AnalyticsState({
    this.overview, this.daily = const [], this.modelCosts = const [],
    this.topQuestions = const [], this.quota, this.isLoading = false,
    this.errorMessage, this.selectedDays = 30,
  });

  AnalyticsState copyWith({
    AnalyticsOverview? overview, List<DailyUsage>? daily, List<ModelCost>? modelCosts,
    List<TopQuestion>? topQuestions, QuotaStats? quota, bool? isLoading,
    String? errorMessage, int? selectedDays,
  }) => AnalyticsState(
    overview: overview ?? this.overview, daily: daily ?? this.daily,
    modelCosts: modelCosts ?? this.modelCosts, topQuestions: topQuestions ?? this.topQuestions,
    quota: quota ?? this.quota, isLoading: isLoading ?? this.isLoading,
    errorMessage: errorMessage, selectedDays: selectedDays ?? this.selectedDays,
  );
}

class AnalyticsNotifier extends StateNotifier<AnalyticsState> {
  AnalyticsNotifier(this._repo) : super(const AnalyticsState());
  final AnalyticsRepository _repo;

  Future<void> load({int? days}) async {
    final d = days ?? state.selectedDays;
    state = state.copyWith(isLoading: true, errorMessage: null, selectedDays: d);
    try {
      final results = await Future.wait([
        _repo.overview(days: d),
        _repo.daily(days: d),
        _repo.modelCosts(days: d),
        _repo.topQuestions(days: d),
        _repo.quota(),
      ]);
      state = AnalyticsState(
        overview: results[0] as AnalyticsOverview,
        daily: results[1] as List<DailyUsage>,
        modelCosts: results[2] as List<ModelCost>,
        topQuestions: results[3] as List<TopQuestion>,
        quota: results[4] as QuotaStats,
        selectedDays: d,
      );
    } on Failure catch (e) { state = state.copyWith(isLoading: false, errorMessage: e.message); }
  }
}

final analyticsProvider = StateNotifierProvider<AnalyticsNotifier, AnalyticsState>((ref) {
  return AnalyticsNotifier(ref.watch(analyticsRepositoryProvider));
});
