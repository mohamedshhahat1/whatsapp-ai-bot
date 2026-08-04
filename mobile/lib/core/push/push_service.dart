import 'dart:async';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

/// Android notification channel id.
///
/// Must match PUSH_ANDROID_CHANNEL on the backend. Android 8+ silently drops a
/// notification whose channel does not exist on the device, so a mismatch here
/// produces delivery with no visible alert -- the hardest failure of this
/// feature to notice.
const String kAndroidChannelId = 'whatsapp_ai_bot_alerts';
const String kAndroidChannelName = 'Customer alerts';

/// Handles a message that arrives while the app is backgrounded or terminated.
///
/// Top-level and annotated on purpose. Android runs this in a fresh Dart
/// isolate with no access to the running app's state, and the annotation keeps
/// the release build's tree shaker from removing an entry point nothing appears
/// to call. A closure or a class method here does not work.
///
/// It deliberately does almost nothing. The message already carries a
/// notification block, so the OS draws it without help; re-displaying it here
/// would show every alert twice.
@pragma('vm:entry-point')
Future<void> firebaseMessagingBackgroundHandler(RemoteMessage message) async {
  await Firebase.initializeApp();
}

/// Firebase Messaging lifecycle: permissions, token, and displaying alerts.
///
/// Knows nothing about the backend. Registration is a callback supplied by
/// [PushService.start], so this class stays testable and the HTTP call lives in
/// the repository where the rest of the API calls live.
class PushService {
  PushService();

  final FlutterLocalNotificationsPlugin _local =
      FlutterLocalNotificationsPlugin();

  /// Conversation ids from tapped notifications.
  ///
  /// A broadcast stream rather than a callback: the tap can arrive before the
  /// widget tree that handles it exists, and several listeners may care.
  final StreamController<int> _taps = StreamController<int>.broadcast();
  Stream<int> get taps => _taps.stream;

  bool _started = false;

  /// Initialise messaging and register this device.
  ///
  /// [onToken] receives the FCM token and the platform string the backend
  /// expects. Called once now and again on every rotation -- Firebase reissues
  /// tokens on reinstall, restore and clear-data, and a stale token is a phone
  /// that silently stops being notified.
  Future<void> start({
    required Future<void> Function(String token, String platform) onToken,
  }) async {
    if (_started) return;
    _started = true;

    await _configureLocalNotifications();

    final messaging = FirebaseMessaging.instance;

    // iOS and Android 13+ both require this. Without it getToken() returns a
    // token on Android and notifications are silently withheld on iOS.
    await messaging.requestPermission(alert: true, badge: true, sound: true);

    // iOS only: APNs delivers the alert, so let the system draw it while the
    // app is in the foreground instead of duplicating it locally.
    await messaging.setForegroundNotificationPresentationOptions(
      alert: true,
      badge: true,
      sound: true,
    );

    final platform = defaultTargetPlatform == TargetPlatform.iOS
        ? 'ios'
        : 'android';

    final token = await messaging.getToken();
    if (token != null && token.isNotEmpty) {
      await onToken(token, platform);
    }
    messaging.onTokenRefresh.listen((refreshed) async {
      await onToken(refreshed, platform);
    });

    // Foreground: Android does NOT display an FCM notification while the app
    // is in front, so it has to be drawn locally or the operator sees nothing.
    FirebaseMessaging.onMessage.listen(_showForeground);

    // Tapped while the app was backgrounded.
    FirebaseMessaging.onMessageOpenedApp.listen(_handleTap);

    // Tapped while the app was terminated: the launch message is available
    // once, at startup, and is lost if it is not read here.
    final initial = await messaging.getInitialMessage();
    if (initial != null) {
      _handleTap(initial);
    }
  }

  Future<void> _configureLocalNotifications() async {
    await _local.initialize(
      const InitializationSettings(
        android: AndroidInitializationSettings('@mipmap/ic_launcher'),
        iOS: DarwinInitializationSettings(),
      ),
      onDidReceiveNotificationResponse: (response) {
        final payload = response.payload;
        if (payload != null) _emit(payload);
      },
    );

    // Created explicitly so the channel exists before the first notification
    // arrives, and so its importance is high enough to appear as a heads-up
    // alert rather than a silent entry in the shade.
    await _local
        .resolvePlatformSpecificImplementation<
            AndroidFlutterLocalNotificationsPlugin>()
        ?.createNotificationChannel(
          const AndroidNotificationChannel(
            kAndroidChannelId,
            kAndroidChannelName,
            description: 'New customer messages, leads and handoffs',
            importance: Importance.high,
          ),
        );
  }

  Future<void> _showForeground(RemoteMessage message) async {
    final notification = message.notification;
    if (notification == null) return;
    await _local.show(
      message.hashCode,
      notification.title,
      notification.body,
      const NotificationDetails(
        android: AndroidNotificationDetails(
          kAndroidChannelId,
          kAndroidChannelName,
          importance: Importance.high,
          priority: Priority.high,
        ),
        iOS: DarwinNotificationDetails(),
      ),
      payload: message.data['conversation_id']?.toString(),
    );
  }

  void _handleTap(RemoteMessage message) {
    _emit(message.data['conversation_id']?.toString());
  }

  void _emit(String? rawId) {
    final id = int.tryParse(rawId ?? '');
    // A malformed or missing id is dropped rather than guessed at. Opening the
    // wrong customer's conversation would be worse than opening none.
    if (id != null) _taps.add(id);
  }

  void dispose() {
    _taps.close();
  }
}
