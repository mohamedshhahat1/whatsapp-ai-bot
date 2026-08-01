import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/error/failures.dart';
import '../../core/network/dio_client.dart';
import '../../core/network/api_endpoints.dart';
import 'analytics_models.dart';

class AnalyticsRepository {
  AnalyticsRepository(this._dio);
  final DioClient _dio;

  Future<AnalyticsOverview> overview({int days = 30}) async {
    try {
      final res = await _dio.get(ApiEndpoints.overview(days: days));
      return AnalyticsOverview.fromJson(res.data as Map<String, dynamic>);
    } on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }

  Future<List<DailyUsage>> daily({int days = 30}) async {
    try {
      final res = await _dio.get(ApiEndpoints.daily(days: days));
      final list = res.data as List;
      return list.map((e) => DailyUsage.fromJson(e as Map<String, dynamic>)).toList();
    } on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }

  Future<List<ModelCost>> modelCosts({int days = 30}) async {
    try {
      final res = await _dio.get(ApiEndpoints.models(days: days));
      final list = res.data as List;
      return list.map((e) => ModelCost.fromJson(e as Map<String, dynamic>)).toList();
    } on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }

  Future<List<TopQuestion>> topQuestions({int days = 30, int limit = 10}) async {
    try {
      final res = await _dio.get(ApiEndpoints.questions(days: days, limit: limit));
      final list = res.data as List;
      return list.map((e) => TopQuestion.fromJson(e as Map<String, dynamic>)).toList();
    } on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }

  Future<QuotaStats> quota() async {
    try {
      final res = await _dio.get(ApiEndpoints.quota());
      return QuotaStats.fromJson(res.data as Map<String, dynamic>);
    } on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }
}

final analyticsRepositoryProvider = Provider<AnalyticsRepository>((ref) {
  return AnalyticsRepository(ref.watch(dioClientProvider));
});
