import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:hive_flutter/hive_flutter.dart';

import 'app.dart';
import 'core/push/push_service.dart';
import 'core/storage/secure_storage.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Initialize Hive for offline cache
  await Hive.initFlutter();
  await Hive.openBox('cache');
  await Hive.openBox('settings');

  // Firebase, for push notifications.
  //
  // Wrapped rather than awaited bare: initialisation throws when the platform
  // config file is missing (google-services.json / GoogleService-Info.plist),
  // and those are generated per developer and never committed. A build without
  // them must still run -- it simply receives no notifications.
  try {
    await Firebase.initializeApp();
    // Registered before runApp and outside the try's success path only because
    // it must be set before any message can arrive. Must be a top-level
    // function; see the note on the handler itself.
    FirebaseMessaging.onBackgroundMessage(firebaseMessagingBackgroundHandler);
  } catch (_) {
    // No Firebase configuration in this build.
  }

  // Preload secure storage to check for existing session
  final secureStorage = SecureStorage();
  final hasKey = await secureStorage.hasApiKey();
  final baseUrl = await secureStorage.getBaseUrl();

  runApp(
    ProviderScope(
      overrides: [
        // overrideWithValue exists on Provider, but riverpod 2.6 removed it
        // from StateProvider, so the two StateProviders use overrideWith.
        secureStorageProvider.overrideWithValue(secureStorage),
        initialAuthStateProvider.overrideWith((ref) => hasKey),
        initialBaseUrlProvider.overrideWith((ref) => baseUrl),
      ],
      child: const WhatsAppAiApp(),
    ),
  );
}
