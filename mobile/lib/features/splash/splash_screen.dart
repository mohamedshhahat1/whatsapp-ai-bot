import 'package:flutter/material.dart';
import 'package:flutter_animate/flutter_animate.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme/app_colors.dart';
import '../auth/auth_provider.dart';

class SplashScreen extends ConsumerStatefulWidget {
  const SplashScreen({super.key});
  @override
  ConsumerState<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends ConsumerState<SplashScreen> {
  @override
  void initState() { super.initState(); _navigate(); }
  Future<void> _navigate() async {
    await Future.delayed(const Duration(milliseconds: 1500));
    if (!mounted) return;
    final isAuth = ref.read(authStateProvider);
    if (isAuth) { context.go('/chats'); } else { context.go('/login'); }
  }
  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
        Container(
          width: 100, height: 100,
          decoration: BoxDecoration(color: AppColors.primary, borderRadius: BorderRadius.circular(28), boxShadow: [BoxShadow(color: AppColors.primary.withValues(alpha: 0.3), blurRadius: 30, offset: const Offset(0, 10))]),
          child: const Icon(Icons.chat, size: 50, color: Colors.white),
        ).animate().scale(duration: 800.ms, curve: Curves.elasticOut),
        const SizedBox(height: 24),
        Text('WhatsApp AI', style: Theme.of(context).textTheme.headlineMedium).animate().fadeIn(delay: 400.ms),
        const SizedBox(height: 8),
        SizedBox(width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 2, color: AppColors.primary.withValues(alpha: 0.5))).animate().fadeIn(delay: 600.ms),
      ])),
    );
  }
}
