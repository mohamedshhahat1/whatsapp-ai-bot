import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/error/failures.dart';
import '../../core/network/dio_client.dart';
import '../../core/network/api_endpoints.dart';
import 'customer_models.dart';

class CustomerRepository {
  CustomerRepository(this._dio);
  final DioClient _dio;

  Future<List<CustomerActivity>> listCustomers({int offset = 0, int limit = 50}) async {
    try {
      final res = await _dio.get(ApiEndpoints.customers(offset: offset, limit: limit));
      final list = res.data as List;
      return list.map((e) => CustomerActivity.fromJson(e as Map<String, dynamic>)).toList();
    } on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }

  Future<List<UserRead>> listUsers({int offset = 0, int limit = 50}) async {
    try {
      final res = await _dio.get(ApiEndpoints.users(offset: offset, limit: limit));
      final list = res.data as List;
      return list.map((e) => UserRead.fromJson(e as Map<String, dynamic>)).toList();
    } on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }

  Future<StatsRead> stats() async {
    try {
      final res = await _dio.get(ApiEndpoints.stats());
      return StatsRead.fromJson(res.data as Map<String, dynamic>);
    } on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }

  Future<UnblockResponse> unblock(String waId) async {
    try {
      final res = await _dio.post(ApiEndpoints.unblock(waId));
      return UnblockResponse.fromJson(res.data as Map<String, dynamic>);
    } on DioException catch (e) { throw e.error ?? UnknownFailure(); }
  }
}

final customerRepositoryProvider = Provider<CustomerRepository>((ref) {
  return CustomerRepository(ref.watch(dioClientProvider));
});
