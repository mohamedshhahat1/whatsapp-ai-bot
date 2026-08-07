import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../config/app_config.dart';
import '../storage/secure_storage.dart';
import 'api_interceptor.dart';

class DioClient {
  DioClient(this._secureStorage);

  final SecureStorage _secureStorage;
  late final Dio _dio = _build();

  Dio _build() {
    final dio = Dio(
      BaseOptions(
        connectTimeout: const Duration(milliseconds: AppConfig.connectTimeoutMs),
        receiveTimeout: const Duration(milliseconds: AppConfig.receiveTimeoutMs),
        sendTimeout: const Duration(milliseconds: AppConfig.sendTimeoutMs),
        validateStatus: (s) => s != null && s < 500,
      ),
    );

    dio.interceptors.addAll([
      AuthInterceptor(_secureStorage),
      ErrorInterceptor(),
      // Debug builds only, and headers are NEVER printed.
      //
      // AuthInterceptor attaches X-API-Key to every request, and that key is
      // unscoped admin access to every customer's phone number and message
      // history. LogInterceptor prints request headers by default, which put it
      // in logcat in full on every call -- and therefore into every bug report,
      // screen recording and pasted debug log.
      //
      // Headers are switched off rather than redacted: url/method/timings are
      // what makes this useful, the credential never was, and a redaction list
      // silently fails to cover the next sensitive header somebody adds.
      if (kDebugMode)
        LogInterceptor(
          request: true,
          requestHeader: false,
          requestBody: false,
          responseHeader: false,
          responseBody: false,
          // debugPrint is throttled; print() drops lines silently once logcat
          // rate-limits, which is worse than not logging at all.
          logPrint: (o) => debugPrint('[DIO] $o'),
        ),
    ]);

    return dio;
  }

  Dio get dio => _dio;

  /// Update the base URL at runtime (after user enters it on login).
  void updateBaseUrl(String baseUrl) {
    final clean = baseUrl.endsWith('/')
        ? baseUrl.substring(0, baseUrl.length - 1)
        : baseUrl;
    _dio.options.baseUrl = '$clean${AppConfig.apiPrefix}';
  }

  Future<Response<T>> get<T>(
    String path, {
    Map<String, dynamic>? query,
    CancelToken? cancelToken,
  }) =>
      _dio.get<T>(path, queryParameters: query, cancelToken: cancelToken);

  Future<Response<T>> post<T>(
    String path, {
    dynamic data,
    CancelToken? cancelToken,
  }) =>
      _dio.post<T>(path, data: data, cancelToken: cancelToken);

  /// DELETE, optionally with a body.
  ///
  /// [data] exists for /device-token, which identifies the device by a token in
  /// the body rather than in the path: a push token is effectively an address
  /// for somebody's phone, and query strings end up in access logs, proxy logs
  /// and browser history in a way request bodies do not.
  Future<Response<T>> delete<T>(
    String path, {
    dynamic data,
    CancelToken? cancelToken,
  }) =>
      _dio.delete<T>(path, data: data, cancelToken: cancelToken);
}

final dioClientProvider = Provider<DioClient>((ref) {
  final storage = ref.watch(secureStorageProvider);
  return DioClient(storage);
});
