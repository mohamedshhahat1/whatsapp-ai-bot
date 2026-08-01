import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

const _keyApiKey = 'admin_api_key';
const _keyBaseUrl = 'base_url';
const _keyOperatorName = 'operator_name';
const _keyThemeMode = 'theme_mode';
const _keyLocale = 'locale';

class SecureStorage {
  SecureStorage();

  final _storage = const FlutterSecureStorage(
    aOptions: AndroidOptions(encryptedSharedPreferences: true),
    iOptions: IOSOptions(accessibility: KeychainAccessibility.first_unlock),
  );

  // --- API Key ---
  Future<void> saveApiKey(String key) => _storage.write(key: _keyApiKey, value: key);
  Future<String?> getApiKey() => _storage.read(key: _keyApiKey);
  Future<bool> hasApiKey() async => (await _storage.read(key: _keyApiKey))?.isNotEmpty ?? false;
  Future<void> clearApiKey() => _storage.delete(key: _keyApiKey);

  // --- Base URL ---
  Future<void> saveBaseUrl(String url) => _storage.write(key: _keyBaseUrl, value: url);
  Future<String?> getBaseUrl() => _storage.read(key: _keyBaseUrl);

  // --- Operator Name ---
  Future<void> saveOperatorName(String name) => _storage.write(key: _keyOperatorName, value: name);
  Future<String?> getOperatorName() => _storage.read(key: _keyOperatorName);

  // --- Theme ---
  Future<void> saveThemeMode(String mode) => _storage.write(key: _keyThemeMode, value: mode);
  Future<String?> getThemeMode() => _storage.read(key: _keyThemeMode);

  // --- Locale ---
  Future<void> saveLocale(String locale) => _storage.write(key: _keyLocale, value: locale);
  Future<String?> getLocale() => _storage.read(key: _keyLocale);

  // --- Clear all ---
  Future<void> clearAll() => _storage.deleteAll();
}

final secureStorageProvider = Provider<SecureStorage>((ref) => SecureStorage());

/// Preloaded at main() — whether a session exists.
final initialAuthStateProvider = StateProvider<bool>((ref) => false);

/// Preloaded at main() — the stored base URL.
final initialBaseUrlProvider = StateProvider<String?>((ref) => null);
