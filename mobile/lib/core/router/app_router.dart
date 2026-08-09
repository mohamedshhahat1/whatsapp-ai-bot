import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../features/splash/splash_screen.dart';
import '../../features/auth/login_screen.dart';
import '../../features/auth/auth_provider.dart';
import '../../features/chat/chat_list_screen.dart';
import '../../features/chat/chat_detail_screen.dart';
import '../../features/customers/customers_screen.dart';
import '../../features/customers/customer_detail_screen.dart';
import '../../features/analytics/analytics_screen.dart';
import '../../features/notifications/notifications_screen.dart';
import '../../features/settings/settings_screen.dart';

final appRouterProvider = Provider<GoRouter>((ref) {
  // Watched, not read, and the value is deliberately discarded: this is what
  // rebuilds the provider -- and therefore the GoRouter -- when the operator
  // logs in or out, so the redirect below runs again against the new state.
  // The redirect itself uses ref.read so it does not subscribe a second time.
  ref.watch(authStateProvider);

  return GoRouter(
    initialLocation: '/splash',
    redirect: (context, state) {
      final isAuth = ref.read(authStateProvider);
      final path = state.matchedLocation;

      if (path == '/splash') return null;
      if (!isAuth && path != '/login') return '/login';
      if (isAuth && path == '/login') return '/chats';
      return null;
    },
    routes: [
      GoRoute(
        path: '/splash',
        builder: (context, state) => const SplashScreen(),
      ),
      GoRoute(
        path: '/login',
        builder: (context, state) => const LoginScreen(),
      ),
      ShellRoute(
        builder: (context, state, child) => MainShell(child: child),
        routes: [
          GoRoute(
            path: '/chats',
            builder: (context, state) => const ChatListScreen(),
            routes: [
              GoRoute(
                path: ':id',
                builder: (context, state) => ChatDetailScreen(
                  conversationId: int.parse(state.pathParameters['id']!),
                ),
              ),
            ],
          ),
          GoRoute(
            path: '/customers',
            builder: (context, state) => const CustomersScreen(),
            routes: [
              GoRoute(
                path: ':waId',
                builder: (context, state) => CustomerDetailScreen(
                  waId: state.pathParameters['waId']!,
                ),
              ),
            ],
          ),
          GoRoute(
            path: '/analytics',
            builder: (context, state) => const AnalyticsScreen(),
          ),
          GoRoute(
            path: '/notifications',
            builder: (context, state) => const NotificationsScreen(),
          ),
          GoRoute(
            path: '/settings',
            builder: (context, state) => const SettingsScreen(),
          ),
        ],
      ),
    ],
  );
});

/// Bottom navigation shell.
class MainShell extends ConsumerWidget {
  const MainShell({super.key, required this.child});
  final Widget child;

  static const _destinations = [
    (icon: Icons.chat_bubble_outline, label: 'Chats', path: '/chats'),
    (icon: Icons.people_outline, label: 'Customers', path: '/customers'),
    (icon: Icons.bar_chart, label: 'Analytics', path: '/analytics'),
    (icon: Icons.notifications_outlined, label: 'Alerts', path: '/notifications'),
    (icon: Icons.settings_outlined, label: 'Settings', path: '/settings'),
  ];

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final location = GoRouterState.of(context).matchedLocation;
    final selectedIndex = _destinations.indexWhere((d) => location.startsWith(d.path));

    return Scaffold(
      body: child,
      bottomNavigationBar: NavigationBar(
        selectedIndex: selectedIndex.clamp(0, 4),
        onDestinationSelected: (i) => context.go(_destinations[i].path),
        destinations: _destinations.map((d) =>
          NavigationDestination(
            icon: Icon(d.icon),
            selectedIcon: Icon(d.icon, color: Theme.of(context).colorScheme.primary),
            label: d.label,
          ),
        ).toList(),
      ),
    );
  }
}
