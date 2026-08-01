import 'package:flutter/material.dart';
import 'package:shimmer/shimmer.dart';

import '../../core/theme/app_colors.dart';

class ChatListShimmer extends StatelessWidget {
  const ChatListShimmer({super.key});
  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    return Shimmer.fromColors(
      baseColor: isDark ? AppColors.darkSurface : AppColors.lightBg,
      highlightColor: isDark ? AppColors.darkBg : Colors.white,
      child: ListView.builder(
        physics: const NeverScrollableScrollPhysics(),
        itemCount: 12,
        itemBuilder: (context, index) => ListTile(
          leading: const CircleAvatar(radius: 28),
          title: Container(height: 14, width: 200, decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(4))),
          subtitle: Container(height: 12, width: 150, decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(4))),
        ),
      ),
    );
  }
}

class CustomerListShimmer extends StatelessWidget {
  const CustomerListShimmer({super.key});
  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    return Shimmer.fromColors(
      baseColor: isDark ? AppColors.darkSurface : AppColors.lightBg,
      highlightColor: isDark ? AppColors.darkBg : Colors.white,
      child: ListView.builder(
        physics: const NeverScrollableScrollPhysics(),
        itemCount: 10,
        itemBuilder: (context, index) => ListTile(
          leading: const CircleAvatar(radius: 24),
          title: Container(height: 14, width: 180, decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(4))),
          subtitle: Container(height: 12, width: 120, decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(4))),
        ),
      ),
    );
  }
}

class AnalyticsShimmer extends StatelessWidget {
  const AnalyticsShimmer({super.key});
  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    return Shimmer.fromColors(
      baseColor: isDark ? AppColors.darkSurface : AppColors.lightBg,
      highlightColor: isDark ? AppColors.darkBg : Colors.white,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(children: [
          GridView.count(shrinkWrap: true, crossAxisCount: 2, childAspectRatio: 1.5, mainAxisSpacing: 8, crossAxisSpacing: 8, children: List.generate(4, (i) => Card(child: Container()))),
          const SizedBox(height: 16),
          Card(child: Container(height: 120)),
          const SizedBox(height: 16),
          Card(child: Container(height: 80)),
        ]),
      ),
    );
  }
}
