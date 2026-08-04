import '../network/api_endpoints.dart';
import '../network/dio_client.dart';

/// Registers and removes this device's push token on the backend.
class PushRepository {
  PushRepository(this._client);

  final DioClient _client;

  /// Tell the backend this device wants notifications.
  ///
  /// [privacy] is 'private' or 'preview' and defaults to private on the server
  /// as well, so an older build that omits it cannot accidentally opt a device
  /// into showing customer names.
  Future<void> register({
    required String token,
    required String platform,
    String privacy = 'private',
  }) async {
    await _client.post<Map<String, dynamic>>(
      ApiEndpoints.deviceToken(),
      data: {
        'token': token,
        'platform': platform,
        'notification_privacy': privacy,
      },
    );
  }

  /// Stop notifications for this device.
  ///
  /// The token travels in the body, not the query string, so it stays out of
  /// access logs and proxy logs.
  Future<void> unregister(String token) async {
    await _client.delete<void>(
      ApiEndpoints.deviceToken(),
      data: {'token': token},
    );
  }
}
