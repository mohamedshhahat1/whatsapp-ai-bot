/// Central configuration for the mobile app.
/// All values are environment-based and can be overridden at runtime.
class AppConfig {
  AppConfig._();

  /// Default base URL — overridden by user input on login screen.
  static const defaultBaseUrl = 'https://api.example.com';

  /// API version prefix — all admin endpoints are under /admin.
  static const apiPrefix = '/admin';

  /// WebSocket path on the backend.
  static const wsPath = '/ws/events';

  /// Network timeouts.
  static const connectTimeoutMs = 10000;
  static const receiveTimeoutMs = 15000;
  static const sendTimeoutMs = 10000;

  /// WebSocket reconnect delays (exponential backoff).
  static const wsInitialReconnectDelay = Duration(seconds: 1);
  static const wsMaxReconnectDelay = Duration(seconds: 30);

  /// Pagination.
  static const pageSize = 50;

  /// Max file upload size (bytes).
  static const maxUploadBytes = 50 * 1024 * 1024; // 50 MB
}

/// Build a WebSocket URL from an HTTP base URL.
String wsUrlFromHttp(String httpBaseUrl) {
  final uri = Uri.parse(httpBaseUrl);
  final wsScheme = uri.scheme == 'https' ? 'wss' : 'ws';
  return Uri(
    scheme: wsScheme,
    host: uri.host,
    port: uri.port == 0 ? null : uri.port,
    path: AppConfig.wsPath,
  ).toString();
}

/// Build an admin API path from a base URL.
String adminPath(String baseUrl, String path) {
  final cleanBase = baseUrl.endsWith('/') ? baseUrl.substring(0, baseUrl.length - 1) : baseUrl;
  return '$cleanBase${AppConfig.apiPrefix}$path';
}
