import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_localizations/flutter_localizations.dart';

import 'core/push/push_provider.dart';
import 'core/router/app_router.dart';
import 'core/theme/app_theme.dart';
import 'features/settings/settings_provider.dart';
import 'l10n/app_localizations.dart';

class WhatsAppAiApp extends ConsumerStatefulWidget {
  const WhatsAppAiApp({super.key});

  @override
  ConsumerState<WhatsAppAiApp> createState() => _WhatsAppAiAppState();
}

class _WhatsAppAiAppState extends ConsumerState<WhatsAppAiApp> {
  @override
  void initState() {
    super.initState();
    // After the first frame, not during build: starting push reads providers
    // and performs I/O, and requesting notification permission during the very
    // first build shows a system dialog over a half-painted screen.
    //
    // Deliberately not in main(): registration needs the base URL and API key,
    // which do not exist until somebody has logged in.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      ref.read(pushBootstrapProvider);
    });
  }

  @override
  Widget build(BuildContext context) {
    final router = ref.watch(appRouterProvider);
    final themeMode = ref.watch(themeModeProvider);
    final locale = ref.watch(localeProvider);

    // A tapped notification opens that conversation directly. Listened for
    // here rather than in a screen, because the tap can arrive while the app
    // is on any screen -- or while it is not running at all, in which case the
    // launch message is replayed once the tree is up.
    ref.listen(pushTapProvider, (previous, next) {
      final conversationId = next.valueOrNull;
      if (conversationId != null) {
        router.go('/chats/$conversationId');
      }
    });

    return MaterialApp.router(
      title: 'WhatsApp AI',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.light(),
      darkTheme: AppTheme.dark(),
      themeMode: themeMode,
      locale: locale,
      supportedLocales: AppLocalizations.supportedLocales,
      localizationsDelegates: const [
        AppLocalizations.delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      routerConfig: router,
    );
  }
}
