import 'package:flutter/material.dart';
import 'package:flutter_animate/flutter_animate.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme/app_colors.dart';
import '../../core/config/app_config.dart';
import 'auth_provider.dart';

class LoginScreen extends ConsumerStatefulWidget {
  const LoginScreen({super.key});

  @override
  ConsumerState<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends ConsumerState<LoginScreen> {
  final _formKey = GlobalKey<FormState>();
  final _urlController = TextEditingController();
  final _keyController = TextEditingController();
  final _nameController = TextEditingController();
  bool _obscureKey = true;
  bool _loading = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    final initialUrl = ref.read(initialBaseUrlProvider);
    if (initialUrl != null && initialUrl.isNotEmpty) {
      _urlController.text = initialUrl;
    } else {
      _urlController.text = AppConfig.defaultBaseUrl;
    }
  }

  @override
  void dispose() {
    _urlController.dispose();
    _keyController.dispose();
    _nameController.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() {
      _loading = true;
      _error = null;
    });

    try {
      await ref.read(authStateProvider.notifier).login(
        _keyController.text.trim(),
        _urlController.text.trim(),
      );
      await ref.read(secureStorageProvider).saveOperatorName(
        _nameController.text.trim(),
      );
      if (mounted) context.go('/chats');
    } catch (e) {
      setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 32),
            child: Form(
              key: _formKey,
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Container(
                    width: 80, height: 80,
                    decoration: BoxDecoration(
                      color: AppColors.primary,
                      borderRadius: BorderRadius.circular(24),
                      boxShadow: [
                        BoxShadow(
                          color: AppColors.primary.withOpacity(0.3),
                          blurRadius: 20,
                          offset: const Offset(0, 8),
                        ),
                      ],
                    ),
                    child: const Icon(Icons.chat, size: 40, color: Colors.white),
                  ).animate().scale(duration: 600.ms).then().shake(),
                  const SizedBox(height: 24),
                  Text('WhatsApp AI', textAlign: TextAlign.center, style: theme.textTheme.headlineMedium).animate().fadeIn(delay: 200.ms),
                  const SizedBox(height: 4),
                  Text('Sign in to manage conversations', textAlign: TextAlign.center, style: theme.textTheme.bodyMedium).animate().fadeIn(delay: 300.ms),
                  const SizedBox(height: 40),
                  TextFormField(
                    controller: _urlController,
                    decoration: const InputDecoration(labelText: 'Server URL', hintText: 'https://your-api.com', prefixIcon: Icon(Icons.dns)),
                    validator: (v) {
                      if (v == null || v.trim().isEmpty) return 'Required';
                      if (!v.startsWith('http://') && !v.startsWith('https://')) return 'Must start with http:// or https://';
                      return null;
                    },
                  ).animate().fadeIn(delay: 400.ms).slideY(begin: 0.1),
                  const SizedBox(height: 16),
                  TextFormField(
                    controller: _keyController,
                    obscureText: _obscureKey,
                    decoration: InputDecoration(
                      labelText: 'API Key', hintText: 'Your admin API key', prefixIcon: const Icon(Icons.key),
                      suffixIcon: IconButton(icon: Icon(_obscureKey ? Icons.visibility : Icons.visibility_off), onPressed: () => setState(() => _obscureKey = !_obscureKey)),
                    ),
                    validator: (v) { if (v == null || v.trim().isEmpty) return 'Required'; return null; },
                  ).animate().fadeIn(delay: 500.ms).slideY(begin: 0.1),
                  const SizedBox(height: 16),
                  TextFormField(
                    controller: _nameController,
                    decoration: const InputDecoration(labelText: 'Your Name (optional)', hintText: 'Used when you take over conversations', prefixIcon: Icon(Icons.person)),
                  ).animate().fadeIn(delay: 600.ms).slideY(begin: 0.1),
                  if (_error != null) ...[
                    const SizedBox(height: 16),
                    Container(
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(color: AppColors.error.withOpacity(0.1), borderRadius: BorderRadius.circular(12)),
                      child: Row(children: [
                        const Icon(Icons.error_outline, color: AppColors.error, size: 20),
                        const SizedBox(width: 8),
                        Expanded(child: Text(_error!, style: TextStyle(color: AppColors.error, fontSize: 13))),
                      ]),
                    ).animate().shake(),
                  ],
                  const SizedBox(height: 32),
                  FilledButton.icon(
                    onPressed: _loading ? null : _submit,
                    icon: _loading ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white)) : const Icon(Icons.login),
                    label: Text(_loading ? 'Signing in...' : 'Sign In'),
                  ).animate().fadeIn(delay: 700.ms).slideY(begin: 0.2),
                  const SizedBox(height: 24),
                  Text('Your API key is stored securely on this device.', textAlign: TextAlign.center, style: theme.textTheme.bodySmall).animate().fadeIn(delay: 800.ms),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
