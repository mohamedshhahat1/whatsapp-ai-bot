import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:hive_flutter/hive_flutter.dart';

import 'app.dart';
import 'core/storage/secure_storage.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Initialize Hive for offline cache
  await Hive.initFlutter();
  await Hive.openBox('cache');
  await Hive.openBox('settings');

  // Preload secure storage to check for existing session
  final secureStorage = SecureStorage();
  final hasKey = await secureStorage.hasApiKey();
  final baseUrl = await secureStorage.getBaseUrl();

  runApp(
    ProviderScope(
      overrides: [
        secureStorageProvider.overrideWithValue(secureStorage),
        initialAuthStateProvider.overrideWithValue(hasKey),
        initialBaseUrlProvider.overrideWithValue(baseUrl),
      ],
      child: const WhatsAppAiApp(),
    ),
  );
}
