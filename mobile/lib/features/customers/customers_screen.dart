import 'package:flutter/material.dart';
import 'package:flutter_animate/flutter_animate.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme/app_colors.dart';
import '../../core/utils/formatters.dart';
import '../../shared/widgets/loading_shimmer.dart';
import '../../shared/widgets/error_view.dart';
import 'customer_provider.dart';

class CustomersScreen extends ConsumerStatefulWidget {
  const CustomersScreen({super.key});
  @override
  ConsumerState<CustomersScreen> createState() => _CustomersScreenState();
}

class _CustomersScreenState extends ConsumerState<CustomersScreen> {
  final _scrollController = ScrollController();
  final _searchController = TextEditingController();
  bool _showSearch = false;

  @override
  void initState() {
    super.initState();
    _scrollController.addListener(_onScroll);
    WidgetsBinding.instance.addPostFrameCallback((_) { ref.read(customerListProvider.notifier).refresh(); });
  }

  @override
  void dispose() { _scrollController.dispose(); _searchController.dispose(); super.dispose(); }

  void _onScroll() {
    if (_scrollController.position.pixels >= _scrollController.position.maxScrollExtent - 200) {
      ref.read(customerListProvider.notifier).loadMore();
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(customerListProvider);
    final notifier = ref.read(customerListProvider.notifier);
    final filtered = state.filtered;
    return Scaffold(
      body: CustomScrollView(
        controller: _scrollController,
        slivers: [
          SliverAppBar(
            pinned: true,
            title: Text('Customers', style: Theme.of(context).textTheme.titleLarge),
            actions: [
              IconButton(
                icon: Icon(_showSearch ? Icons.close : Icons.search),
                onPressed: () => setState(() {
                  _showSearch = !_showSearch;
                  if (!_showSearch) { _searchController.clear(); notifier.setSearch(null); }
                }),
              ),
            ],
          ),
          if (_showSearch)
            SliverToBoxAdapter(
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                child: TextField(
                  controller: _searchController,
                  decoration: const InputDecoration(hintText: 'Search by name or phone...', prefixIcon: Icon(Icons.search, size: 20), isDense: true),
                  onChanged: notifier.setSearch,
                ).animate().fadeIn().slideY(begin: -0.1),
              ),
            ),
          if (state.isLoading && state.customers.isEmpty)
            const SliverToBoxAdapter(child: CustomerListShimmer())
          else if (state.errorMessage != null && state.customers.isEmpty)
            SliverToBoxAdapter(child: ErrorView(message: state.errorMessage!, onRetry: () => notifier.refresh()))
          else if (filtered.isEmpty)
            const SliverToBoxAdapter(child: _EmptyState(icon: Icons.people_outline, title: 'No customers yet', subtitle: 'Customers will appear here when they message your bot.'))
          else
            SliverList.builder(
              itemCount: filtered.length + (state.hasMore ? 1 : 0),
              itemBuilder: (context, index) {
                if (index >= filtered.length) { return const Padding(padding: EdgeInsets.all(16), child: Center(child: CircularProgressIndicator())); }
                final c = filtered[index];
                return ListTile(
                  onTap: () => context.push('/customers/${c.waId}'),
                  leading: CircleAvatar(backgroundColor: AppColors.primary, child: Text(Formatters.initials(c.name, '#'), style: const TextStyle(color: Colors.white))),
                  title: Text(c.name ?? 'Unknown'),
                  subtitle: Text(Formatters.formatPhone(c.waId)),
                  trailing: Column(
                    mainAxisAlignment: MainAxisAlignment.center,
                    crossAxisAlignment: CrossAxisAlignment.end,
                    children: [
                      Text('${c.conversations} conv', style: Theme.of(context).textTheme.bodySmall),
                      Text('${c.messages} msg', style: Theme.of(context).textTheme.bodySmall),
                    ],
                  ),
                ).animate().fadeIn(duration: 300.ms).slideY(begin: 0.05);
              },
            ),
        ],
      ),
    );
  }
}

class _EmptyState extends StatelessWidget {
  final IconData icon; final String title; final String subtitle;
  const _EmptyState({required this.icon, required this.title, required this.subtitle});
  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.all(48),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(icon, size: 64, color: theme.colorScheme.onSurfaceVariant.withValues(alpha: 0.3)).animate().scale(duration: 600.ms),
          const SizedBox(height: 16),
          Text(title, style: theme.textTheme.titleMedium),
          const SizedBox(height: 8),
          Text(subtitle, textAlign: TextAlign.center, style: theme.textTheme.bodySmall),
        ],
      ),
    );
  }
}
