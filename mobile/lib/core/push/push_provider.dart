import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../network/dio_client.dart';
import 'push_repository.dart';
import 'push_service.dart';

final pushRepositoryProvider = Provider<PushRepository>((ref) {
  return PushRepository(ref.watch(dioClientProvider));
});

final pushServiceProvider = Provider<PushService>((ref) {
  final service = PushService();
  ref.onDispose(service.dispose);
  return service;
});

/// Conversation ids from tapped notifications.
///
/// Exposed as a stream provider so the widget tree can listen without holding
/// the service, and so a tap that arrives before the UI is ready is not lost.
final pushTapProvider = StreamProvider<int>((ref) {
  return ref.watch(pushServiceProvider).taps;
});

/// Start Firebase Messaging and register this device.
///
/// Failures are swallowed on purpose. Push is an enhancement: an operator whose
/// token could not be registered -- no Firebase config in the build, no network,
/// not logged in yet -- must still get a working app, the conversation list and
/// the WebSocket.
final pushBootstrapProvider = FutureProvider<void>((ref) async {
  final service = ref.watch(pushServiceProvider);
  final repository = ref.watch(pushRepositoryProvider);
  try {
    await service.start(
      onToken: (token, platform) async {
        try {
          await repository.register(token: token, platform: platform);
        } catch (_) {
          // Registration is retried on the next launch and on the next token
          // rotation; a failed attempt is not worth surfacing to the operator.
        }
      },
    );
  } catch (_) {
    // Most likely no google-services.json in this build.
  }
});
