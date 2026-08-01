import 'package:flutter/material.dart';
import 'package:flutter_animate/flutter_animate.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/theme/app_colors.dart';
import '../../core/utils/formatters.dart';
import '../../shared/widgets/loading_shimmer.dart';
import '../../shared/widgets/error_view.dart';
import 'analytics_provider.dart';
import 'analytics_models.dart';

class AnalyticsScreen extends ConsumerStatefulWidget {
  const AnalyticsScreen({super.key});
  @override
  ConsumerState<AnalyticsScreen> createState() => _AnalyticsScreenState();
}

class _AnalyticsScreenState extends ConsumerState<AnalyticsScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) { ref.read(analyticsProvider.notifier).load(); });
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(analyticsProvider);
    final theme = Theme.of(context);
    return Scaffold(
      body: CustomScrollView(slivers: [
        SliverAppBar(
          pinned: true,
          title: Text('Analytics', style: theme.textTheme.titleLarge),
          actions: [
            PopupMenuButton<int>(
              icon: const Icon(Icons.calendar_month),
              onSelected: (days) => ref.read(analyticsProvider.notifier).load(days: days),
              itemBuilder: (_) => [
                const PopupMenuItem(value: 7, child: Text('7 days')),
                const PopupMenuItem(value: 30, child: Text('30 days')),
                const PopupMenuItem(value: 90, child: Text('90 days')),
                const PopupMenuItem(value: 365, child: Text('1 year')),
              ],
            ),
          ],
        ),
        if (state.isLoading)
          const SliverToBoxAdapter(child: AnalyticsShimmer())
        else if (state.errorMessage != null)
          SliverToBoxAdapter(child: ErrorView(message: state.errorMessage!, onRetry: () => ref.read(analyticsProvider.notifier).load()))
        else if (state.overview != null)
          SliverToBoxAdapter(
            child: Padding(padding: const EdgeInsets.all(16), child: Column(children: [
              Text('Last ${state.selectedDays} days', style: theme.textTheme.bodySmall).animate().fadeIn(),
              const SizedBox(height: 16),
              GridView.count(shrinkWrap: true, physics: const NeverScrollableScrollPhysics(), crossAxisCount: 2, childAspectRatio: 1.5, mainAxisSpacing: 8, crossAxisSpacing: 8, children: [
                _KpiCard(title: 'Total Users', value: Formatters.compact(state.overview!.totalUsers), icon: Icons.people, color: AppColors.info),
                _KpiCard(title: 'Conversations', value: Formatters.compact(state.overview!.totalConversations), icon: Icons.chat, color: AppColors.primary),
                _KpiCard(title: 'Messages', value: Formatters.compact(state.overview!.totalMessages), icon: Icons.message, color: AppColors.success),
                _KpiCard(title: 'AI Requests', value: Formatters.compact(state.overview!.aiRequests), icon: Icons.smart_toy, color: AppColors.botMode),
              ]).animate().fadeIn().slideY(begin: 0.1),
              const SizedBox(height: 16),
              _CostCard(overview: state.overview!).animate().fadeIn(delay: 200.ms).slideY(begin: 0.1),
              const SizedBox(height: 16),
              if (state.quota != null) _QuotaCard(quota: state.quota!).animate().fadeIn(delay: 300.ms).slideY(begin: 0.1),
              const SizedBox(height: 16),
              if (state.daily.isNotEmpty) _DailyChart(daily: state.daily).animate().fadeIn(delay: 400.ms).slideY(begin: 0.1),
              const SizedBox(height: 16),
              if (state.modelCosts.isNotEmpty) _ModelCostList(costs: state.modelCosts).animate().fadeIn(delay: 500.ms).slideY(begin: 0.1),
              const SizedBox(height: 16),
              if (state.topQuestions.isNotEmpty) _TopQuestionsList(questions: state.topQuestions).animate().fadeIn(delay: 600.ms).slideY(begin: 0.1),
            ]),
          ),
        ],
      ),
    );
  }
}

class _KpiCard extends StatelessWidget {
  final String title; final String value; final IconData icon; final Color color;
  const _KpiCard({required this.title, required this.value, required this.icon, required this.color});
  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(child: Padding(padding: const EdgeInsets.all(12), child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
      Row(children: [Icon(icon, size: 18, color: color), const SizedBox(width: 6), Text(title, style: theme.textTheme.bodySmall)]),
      Text(value, style: theme.textTheme.headlineMedium),
    ])));
  }
}

class _CostCard extends StatelessWidget {
  final AnalyticsOverview overview;
  const _CostCard({required this.overview});
  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(child: Padding(padding: const EdgeInsets.all(16), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text('Cost Breakdown', style: theme.textTheme.titleMedium),
      const SizedBox(height: 12),
      _CostRow(label: 'Input Cost', value: Formatters.usd(overview.cost.inputCostUsd)),
      _CostRow(label: 'Output Cost', value: Formatters.usd(overview.cost.outputCostUsd)),
      const Divider(),
      _CostRow(label: 'Total Cost', value: Formatters.usd(overview.cost.totalCostUsd), bold: true),
      _CostRow(label: 'Per Conversation', value: Formatters.usd(overview.costPerConversationUsd)),
      _CostRow(label: 'Projected Monthly', value: Formatters.usd(overview.projectedMonthlyCostUsd), bold: true, color: AppColors.warning),
    ])));
  }
}

class _CostRow extends StatelessWidget {
  final String label; final String value; final bool bold; final Color? color;
  const _CostRow({required this.label, required this.value, this.bold = false, this.color});
  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(padding: const EdgeInsets.symmetric(vertical: 4), child: Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
      Text(label, style: bold ? theme.textTheme.titleMedium : theme.textTheme.bodyMedium),
      Text(value, style: bold ? theme.textTheme.titleMedium?.copyWith(color: color) : theme.textTheme.bodyMedium?.copyWith(color: color)),
    ]));
  }
}

class _QuotaCard extends StatelessWidget {
  final QuotaStats quota;
  const _QuotaCard({required this.quota});
  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    if (!quota.available) {
      return Card(child: Padding(padding: const EdgeInsets.all(16), child: Row(children: [const Icon(Icons.warning, color: AppColors.error), const SizedBox(width: 8), Expanded(child: Text(quota.error ?? 'Quota data unavailable', style: theme.textTheme.bodyMedium))])));
    }
    return Card(child: Padding(padding: const EdgeInsets.all(16), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text('System Health', style: theme.textTheme.titleMedium),
      const SizedBox(height: 12),
      if (quota.aiDisabled == true)
        Container(padding: const EdgeInsets.all(8), decoration: BoxDecoration(color: AppColors.error.withOpacity(0.1), borderRadius: BorderRadius.circular(8)), child: const Row(children: [Icon(Icons.pause_circle, color: AppColors.error, size: 18), SizedBox(width: 6), Text('AI is disabled', style: TextStyle(color: AppColors.error, fontWeight: FontWeight.w600))])),
      const SizedBox(height: 8),
      if (quota.spendUsd != null && quota.spendLimitUsd != null)
        Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('Spend: ${Formatters.usd(quota.spendUsd!)} / ${Formatters.usd(quota.spendLimitUsd!)}', style: theme.textTheme.bodyMedium),
          const SizedBox(height: 4),
          LinearProgressIndicator(value: quota.spendUsedFraction ?? 0, backgroundColor: theme.colorScheme.surfaceContainerHighest, color: (quota.spendUsedFraction ?? 0) > 0.8 ? AppColors.error : AppColors.success),
        ]),
      if (quota.blockedCustomers != null && quota.blockedCustomers! > 0) ...[
        const SizedBox(height: 8),
        Text('${quota.blockedCustomers} blocked customers', style: theme.textTheme.bodySmall?.copyWith(color: AppColors.warning)),
      ],
    ])));
  }
}

class _DailyChart extends StatelessWidget {
  final List<DailyUsage> daily;
  const _DailyChart({required this.daily});
  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final maxMessages = daily.map((d) => d.messages).fold(0, (a, b) => a > b ? a : b);
    return Card(child: Padding(padding: const EdgeInsets.all(16), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text('Daily Activity', style: theme.textTheme.titleMedium),
      const SizedBox(height: 16),
      SizedBox(height: 120, child: Row(crossAxisAlignment: CrossAxisAlignment.end, children: daily.take(30).map((d) {
        final h = maxMessages > 0 ? (d.messages / maxMessages) * 100 : 0.0;
        return Expanded(child: Padding(padding: const EdgeInsets.symmetric(horizontal: 1), child: Tooltip(message: '${d.day}: ${d.messages} msgs', child: FractionallySizedBox(heightFactor: h.clamp(0.02, 1.0), child: Container(decoration: BoxDecoration(color: AppColors.primary.withOpacity(0.7), borderRadius: const BorderRadius.vertical(top: Radius.circular(2))))))));
      }).toList())),
    ])));
  }
}

class _ModelCostList extends StatelessWidget {
  final List<ModelCost> costs;
  const _ModelCostList({required this.costs});
  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(child: Padding(padding: const EdgeInsets.all(16), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text('Cost by Model', style: theme.textTheme.titleMedium),
      const SizedBox(height: 8),
      ...costs.map((c) => ListTile(dense: true, contentPadding: EdgeInsets.zero, title: Text(c.model), subtitle: Text('${Formatters.compact(c.totalTokens)} tokens · ${c.requests} requests'), trailing: Text(Formatters.usd(c.costUsd), style: theme.textTheme.titleMedium))),
    ])));
  }
}

class _TopQuestionsList extends StatelessWidget {
  final List<TopQuestion> questions;
  const _TopQuestionsList({required this.questions});
  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(child: Padding(padding: const EdgeInsets.all(16), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text('Top Questions', style: theme.textTheme.titleMedium),
      const SizedBox(height: 8),
      ...questions.asMap().entries.map((entry) {
        final i = entry.key; final q = entry.value;
        return ListTile(dense: true, contentPadding: EdgeInsets.zero, leading: CircleAvatar(radius: 14, backgroundColor: AppColors.primary.withOpacity(0.1), child: Text('${i + 1}', style: TextStyle(fontSize: 12, color: AppColors.primary, fontWeight: FontWeight.w600))), title: Text(q.question, maxLines: 2, overflow: TextOverflow.ellipsis), subtitle: Text('${q.count} times · ${Formatters.chatTime(q.lastAsked)}'));
      }),
    ])));
  }
}
