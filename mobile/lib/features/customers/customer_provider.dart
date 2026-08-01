import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/config/app_config.dart';
import '../../core/error/failures.dart';
import 'customer_models.dart';
import 'customer_repository.dart';

class CustomerListState {
  final List<CustomerActivity> customers;
  final bool isLoading;
  final bool isLoadingMore;
  final String? errorMessage;
  final bool hasMore;
  final int offset;
  final String? searchQuery;

  const CustomerListState({
    this.customers = const [], this.isLoading = false, this.isLoadingMore = false,
    this.errorMessage, this.hasMore = true, this.offset = 0, this.searchQuery,
  });

  CustomerListState copyWith({
    List<CustomerActivity>? customers, bool? isLoading, bool? isLoadingMore,
    String? errorMessage, bool? hasMore, int? offset, String? searchQuery,
  }) => CustomerListState(
    customers: customers ?? this.customers, isLoading: isLoading ?? this.isLoading,
    isLoadingMore: isLoadingMore ?? this.isLoadingMore, errorMessage: errorMessage,
    hasMore: hasMore ?? this.hasMore, offset: offset ?? this.offset, searchQuery: searchQuery ?? this.searchQuery,
  );

  List<CustomerActivity> get filtered {
    if (searchQuery == null || searchQuery!.isEmpty) return customers;
    final q = searchQuery!.toLowerCase();
    return customers.where((c) { return c.waId.contains(q) || c.name?.toLowerCase().contains(q) == true || c.userId.toString().contains(q); }).toList();
  }
}

class CustomerListNotifier extends StateNotifier<CustomerListState> {
  CustomerListNotifier(this._repo) : super(const CustomerListState());
  final CustomerRepository _repo;

  Future<void> refresh() async {
    state = state.copyWith(isLoading: true, errorMessage: null);
    try {
      final customers = await _repo.listCustomers(offset: 0, limit: AppConfig.pageSize);
      state = CustomerListState(customers: customers, hasMore: customers.length >= AppConfig.pageSize, offset: customers.length);
    } on Failure catch (e) { state = state.copyWith(isLoading: false, errorMessage: e.message); }
  }

  Future<void> loadMore() async {
    if (!state.hasMore || state.isLoadingMore) return;
    state = state.copyWith(isLoadingMore: true);
    try {
      final more = await _repo.listCustomers(offset: state.offset, limit: AppConfig.pageSize);
      state = state.copyWith(customers: [...state.customers, ...more], isLoadingMore: false, hasMore: more.length >= AppConfig.pageSize, offset: state.offset + more.length);
    } on Failure catch (e) { state = state.copyWith(isLoadingMore: false, errorMessage: e.message); }
  }

  void setSearch(String? query) { state = state.copyWith(searchQuery: query); }

  Future<bool> unblock(String waId) async {
    try { await _repo.unblock(waId); return true; }
    on Failure catch (e) { state = state.copyWith(errorMessage: e.message); return false; }
  }
}

final customerListProvider = StateNotifierProvider<CustomerListNotifier, CustomerListState>((ref) {
  return CustomerListNotifier(ref.watch(customerRepositoryProvider));
});
