import 'package:flutter/material.dart';
import 'package:flutter_animate/flutter_animate.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/theme/app_colors.dart';
import '../../core/utils/formatters.dart';
import 'customer_provider.dart';

class CustomerDetailScreen extends ConsumerStatefulWidget {
  final String waId;
  const CustomerDetailScreen({super.key, required this.waId});
  @override
  ConsumerState<CustomerDetailScreen> createState() => _CustomerDetailScreenState();
}

class _CustomerDetailScreenState extends ConsumerState<CustomerDetailScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) { ref.read(customerListProvider.notifier).refresh(); });
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(customerListProvider);
    final customer = state.customers.where((c) => c.waId == widget.waId).firstOrNull;
    return Scaffold(
      body: CustomScrollView(
        slivers: [
          SliverAppBar(
            expandedHeight: 200, pinned: true,
            flexibleSpace: FlexibleSpaceBar(
              title: Text(customer?.name ?? 'Customer', style: const TextStyle(fontSize: 16)),
              background: Container(
                decoration: BoxDecoration(gradient: LinearGradient(begin: Alignment.topCenter, end: Alignment.bottomCenter, colors: [AppColors.primary.withValues(alpha: 0.3), Colors.transparent])),
                child: const Center(child: Icon(Icons.person, size: 64, color: Colors.white70)),
              ),
            ),
          ),
          if (customer != null)
            SliverToBoxAdapter(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(children: [
                  _InfoCard(icon: Icons.phone, label: 'WhatsApp Number', value: Formatters.formatPhone(customer.waId)).animate().fadeIn().slideY(begin: 0.1),
                  const SizedBox(height: 8),
                  _InfoCard(icon: Icons.chat, label: 'Conversations', value: customer.conversations.toString()).animate().fadeIn(delay: 100.ms).slideY(begin: 0.1),
                  const SizedBox(height: 8),
                  _InfoCard(icon: Icons.message, label: 'Total Messages', value: customer.messages.toString()).animate().fadeIn(delay: 200.ms).slideY(begin: 0.1),
                  if (customer.lastActive != null) ...[
                    const SizedBox(height: 8),
                    _InfoCard(icon: Icons.access_time, label: 'Last Active', value: Formatters.fullDate(customer.lastActive!)).animate().fadeIn(delay: 300.ms).slideY(begin: 0.1),
                  ],
                  const SizedBox(height: 16),
                  FilledButton.icon(
                    onPressed: () async {
                      final ok = await ref.read(customerListProvider.notifier).unblock(widget.waId);
                      if (context.mounted) { ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(ok ? 'Customer unblocked' : 'Failed to unblock'))); }
                    },
                    icon: const Icon(Icons.lock_open), label: const Text('Unblock Customer'),
                    style: FilledButton.styleFrom(backgroundColor: AppColors.warning, minimumSize: const Size.fromHeight(48)),
                  ).animate().fadeIn(delay: 400.ms),
                ]),
              ),
            )
          else
            const SliverToBoxAdapter(child: Center(child: CircularProgressIndicator())),
        ],
      ),
    );
  }
}

class _InfoCard extends StatelessWidget {
  final IconData icon; final String label; final String value;
  const _InfoCard({required this.icon, required this.label, required this.value});
  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(children: [
          Icon(icon, color: theme.colorScheme.primary),
          const SizedBox(width: 12),
          Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [Text(label, style: theme.textTheme.bodySmall), Text(value, style: theme.textTheme.titleMedium)])),
        ]),
      ),
    );
  }
}
