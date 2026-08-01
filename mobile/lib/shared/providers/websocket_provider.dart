import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/storage/secure_storage.dart';
import '../../core/websocket/websocket_service.dart';
import '../../features/auth/auth_provider.dart';

final wsConnectionProvider = Provider<void>((ref) {
  final isAuth = ref.watch(authStateProvider);
  final wsService = ref.watch(webSocketServiceProvider);
  final storage = ref.watch(secureStorageProvider);
  if (isAuth) {
    storage.getBaseUrl().then((baseUrl) {
      if (baseUrl != null && baseUrl.isNotEmpty) { wsService.connect(baseUrl); }
    });
  } else { wsService.disconnect(); }
});

final wsConnectionStateProvider = StreamProvider<WsConnectionState>((ref) {
  final wsService = ref.watch(webSocketServiceProvider);
  return wsService.stateStream;
});
