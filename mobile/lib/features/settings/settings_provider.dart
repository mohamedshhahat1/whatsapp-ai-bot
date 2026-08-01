import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/storage/secure_storage.dart';

final themeModeProvider = StateNotifierProvider<ThemeModeNotifier, ThemeMode>((ref) {
  return ThemeModeNotifier(ref.watch(secureStorageProvider));
});

class ThemeModeNotifier extends StateNotifier<ThemeMode> {
  ThemeModeNotifier(this._storage) : super(ThemeMode.system) { _load(); }
  final SecureStorage _storage;
  Future<void> _load() async {
    final mode = await _storage.getThemeMode();
    state = switch (mode) { 'light' => ThemeMode.light, 'dark' => ThemeMode.dark, _ => ThemeMode.system };
  }
  Future<void> set(ThemeMode mode) async { state = mode; await _storage.saveThemeMode(mode.name); }
}

final localeProvider = StateNotifierProvider<LocaleNotifier, Locale>((ref) {
  return LocaleNotifier(ref.watch(secureStorageProvider));
});

class LocaleNotifier extends StateNotifier<Locale> {
  LocaleNotifier(this._storage) : super(const Locale('en')) { _load(); }
  final SecureStorage _storage;
  Future<void> _load() async {
    final loc = await _storage.getLocale();
    state = switch (loc) { 'ar' => const Locale('ar'), _ => const Locale('en') };
  }
  Future<void> set(Locale locale) async { state = locale; await _storage.saveLocale(locale.languageCode); }
}
