import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/storage/secure_storage.dart';
import '../../core/network/dio_client.dart';

/// Auth state — true when the user has a valid API key stored.
final authStateProvider = StateNotifierProvider<AuthNotifier, bool>((ref) {
  final storage = ref.watch(secureStorageProvider);
  final dio = ref.watch(dioClientProvider);
  final initial = ref.watch(initialAuthStateProvider);
  return AuthNotifier(storage, dio, initial);
});

class AuthNotifier extends StateNotifier<bool> {
  AuthNotifier(this._storage, this._dio, bool initial) : super(initial) {
    if (initial) {
      _restoreSession();
    }
  }

  final SecureStorage _storage;
  final DioClient _dio;

  Future<void> _restoreSession() async {
    final baseUrl = await _storage.getBaseUrl();
    if (baseUrl != null && baseUrl.isNotEmpty) {
      _dio.updateBaseUrl(baseUrl);
    }
  }

  /// Login with API key and base URL.
  Future<void> login(String apiKey, String baseUrl) async {
    await _storage.saveApiKey(apiKey);
    await _storage.saveBaseUrl(baseUrl);
    _dio.updateBaseUrl(baseUrl);
    state = true;
  }

  /// Logout and clear all stored credentials.
  Future<void> logout() async {
    await _storage.clearApiKey();
    state = false;
  }
}
