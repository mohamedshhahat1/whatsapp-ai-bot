import 'package:flutter/material.dart';
import 'package:flutter_animate/flutter_animate.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme/app_colors.dart';
import '../../core/storage/secure_storage.dart';
import '../auth/auth_provider.dart';
import 'settings_provider.dart';

class SettingsScreen extends ConsumerStatefulWidget {
  const SettingsScreen({super.key});
  @override
  ConsumerState<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends ConsumerState<SettingsScreen> {
  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final themeMode = ref.watch(themeModeProvider);
    final locale = ref.watch(localeProvider);
    final storage = ref.read(secureStorageProvider);
    return Scaffold(
      body: CustomScrollView(
        slivers: [
          SliverAppBar(pinned: true, title: Text('Settings', style: theme.textTheme.titleLarge)),
          SliverToBoxAdapter(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                children: [
                  FutureBuilder<String?>(
                    future: storage.getOperatorName(),
                    builder: (context, snapshot) {
                      final name = snapshot.data ?? 'Operator';
                      return Card(
                        child: Padding(
                          padding: const EdgeInsets.all(16),
                          child: Row(
                            children: [
                              CircleAvatar(radius: 28, backgroundColor: AppColors.primary, child: Text(name.isNotEmpty ? name[0].toUpperCase() : '?', style: const TextStyle(color: Colors.white, fontSize: 20, fontWeight: FontWeight.w600))),
                              const SizedBox(width: 12),
                              Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [Text(name, style: theme.textTheme.titleMedium), Text('Operator', style: theme.textTheme.bodySmall)])),
                            ],
                          ),
                        ),
                      ).animate().fadeIn().slideY(begin: 0.1);
                    },
                  ),
                  const SizedBox(height: 16),
                  const _SectionHeader(title: 'Appearance'),
                  const SizedBox(height: 8),
                  Card(
                    child: ListTile(
                      leading: const Icon(Icons.dark_mode), title: const Text('Dark Mode'),
                      trailing: DropdownButton<ThemeMode>(
                        value: themeMode,
                        underline: const SizedBox(),
                        onChanged: (mode) { if (mode != null) ref.read(themeModeProvider.notifier).set(mode); },
                        items: const [
                          DropdownMenuItem(value: ThemeMode.system, child: Text('System')),
                          DropdownMenuItem(value: ThemeMode.light, child: Text('Light')),
                          DropdownMenuItem(value: ThemeMode.dark, child: Text('Dark')),
                        ],
                      ),
                    ),
                  ).animate().fadeIn(delay: 100.ms).slideY(begin: 0.1),
                  const SizedBox(height: 16),
                  const _SectionHeader(title: 'Language'),
                  const SizedBox(height: 8),
                  Card(
                    child: ListTile(
                      leading: const Icon(Icons.language), title: const Text('App Language'),
                      trailing: DropdownButton<Locale>(
                        value: locale,
                        underline: const SizedBox(),
                        onChanged: (loc) { if (loc != null) ref.read(localeProvider.notifier).set(loc); },
                        items: const [
                          DropdownMenuItem(value: Locale('en'), child: Text('English')),
                          DropdownMenuItem(value: Locale('ar'), child: Text('العربية')),
                        ],
                      ),
                    ),
                  ).animate().fadeIn(delay: 200.ms).slideY(begin: 0.1),
                  const SizedBox(height: 16),
                  const _SectionHeader(title: 'About'),
                  const SizedBox(height: 8),
                  Card(
                    child: Column(
                      children: [
                        ListTile(leading: const Icon(Icons.info_outline), title: const Text('About'), trailing: const Icon(Icons.chevron_right), onTap: () => _showAbout(context)),
                        ListTile(leading: const Icon(Icons.privacy_tip_outlined), title: const Text('Privacy'), trailing: const Icon(Icons.chevron_right), onTap: () => _showPrivacy(context)),
                      ],
                    ),
                  ).animate().fadeIn(delay: 300.ms).slideY(begin: 0.1),
                  const SizedBox(height: 24),
                  FilledButton.icon(
                    onPressed: () async {
                      final confirmed = await _confirmLogout(context);
                      if (confirmed == true) { await ref.read(authStateProvider.notifier).logout(); if (context.mounted) context.go('/login'); }
                    },
                    icon: const Icon(Icons.logout), label: const Text('Logout'),
                    style: FilledButton.styleFrom(backgroundColor: AppColors.error, minimumSize: const Size.fromHeight(48)),
                  ).animate().fadeIn(delay: 400.ms),
                  const SizedBox(height: 16),
                  Text('WhatsApp AI Mobile v1.0.0', style: theme.textTheme.bodySmall).animate().fadeIn(delay: 500.ms),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  void _showAbout(BuildContext context) {
    showDialog(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('About'),
        content: const Text('WhatsApp AI Mobile\n\nA premium mobile client for managing WhatsApp AI Bot conversations. Built with Flutter, Riverpod, and Material 3.'),
        actions: [TextButton(onPressed: () => Navigator.pop(context), child: const Text('Close'))],
      ),
    );
  }

  void _showPrivacy(BuildContext context) {
    showDialog(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Privacy'),
        content: const Text('Your API key is stored securely using platform keychain (iOS Keychain / Android EncryptedSharedPreferences). No data is sent to any server other than your configured backend. All communication uses HTTPS/WSS.'),
        actions: [TextButton(onPressed: () => Navigator.pop(context), child: const Text('Close'))],
      ),
    );
  }

  Future<bool?> _confirmLogout(BuildContext context) {
    return showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Logout?'),
        content: const Text('You will need to re-enter your API key to sign back in.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), style: FilledButton.styleFrom(backgroundColor: AppColors.error), child: const Text('Logout')),
        ],
      ),
    );
  }
}

class _SectionHeader extends StatelessWidget {
  final String title;
  const _SectionHeader({required this.title});
  @override
  Widget build(BuildContext context) {
    return Padding(padding: const EdgeInsets.only(left: 4), child: Text(title, style: Theme.of(context).textTheme.labelSmall?.copyWith(fontWeight: FontWeight.w700)));
  }
}
