import 'package:dio/dio.dart';

import '../error/failures.dart';
import '../storage/secure_storage.dart';

/// Adds the X-API-Key header to every request.
class AuthInterceptor extends Interceptor {
  AuthInterceptor(this._storage);
  final SecureStorage _storage;

  @override
  Future<void> onRequest(
    RequestOptions options,
    RequestInterceptorHandler handler,
  ) async {
    final key = await _storage.getApiKey();
    if (key != null && key.isNotEmpty) {
      options.headers['X-API-Key'] = key;
    }
    options.headers['Content-Type'] = 'application/json';
    handler.next(options);
  }
}

/// Converts HTTP errors into typed Failures.
class ErrorInterceptor extends Interceptor {
  @override
  void onError(DioException err, ErrorInterceptorHandler handler) {
    switch (err.type) {
      case DioExceptionType.connectionTimeout:
      case DioExceptionType.sendTimeout:
      case DioExceptionType.receiveTimeout:
        handler.reject(
          DioException(
            requestOptions: err.requestOptions,
            type: err.type,
            error: const TimeoutFailure(),
          ),
        );
        return;
      case DioExceptionType.connectionError:
        handler.reject(
          DioException(
            requestOptions: err.requestOptions,
            type: err.type,
            error: const OfflineFailure(),
          ),
        );
        return;
      case DioExceptionType.badResponse:
        final status = err.response?.statusCode ?? 500;
        final detail = _extractDetail(err.response);
        if (status == 401) {
          handler.reject(
            DioException(
              requestOptions: err.requestOptions,
              type: err.type,
              error: UnauthorizedFailure(message: detail),
            ),
          );
          return;
        }
        if (status == 404) {
          handler.reject(
            DioException(
              requestOptions: err.requestOptions,
              type: err.type,
              error: NotFoundFailure(message: detail),
            ),
          );
          return;
        }
        handler.reject(
          DioException(
            requestOptions: err.requestOptions,
            type: err.type,
            error: ServerFailure(message: detail, statusCode: status),
          ),
        );
        return;
      default:
        handler.reject(
          DioException(
            requestOptions: err.requestOptions,
            type: err.type,
            error: UnknownFailure(message: err.message ?? 'Unknown error'),
          ),
        );
    }
  }

  String _extractDetail(Response<dynamic>? response) {
    if (response?.data is Map) {
      final data = response!.data as Map;
      if (data.containsKey('detail')) return data['detail'].toString();
    }
    return response?.statusMessage ?? 'Unknown error';
  }
}
