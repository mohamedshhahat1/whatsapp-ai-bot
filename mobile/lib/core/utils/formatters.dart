import 'package:intl/intl.dart';

class Formatters {
  Formatters._();

  /// Format a WhatsApp wa_id (digits) into a readable phone number.
  static String formatPhone(String waId) {
    if (waId.length >= 10) {
      final country = waId.substring(0, waId.length - 10);
      final number = waId.substring(waId.length - 10);
      final area = number.substring(0, 3);
      final prefix = number.substring(3, 6);
      final line = number.substring(6);
      return '+$country $area-$prefix-$line';
    }
    return '+$waId';
  }

  /// Format an ISO timestamp into a chat list time (e.g., "14:32" or "Aug 3").
  static String chatTime(String isoString) {
    final dt = DateTime.parse(isoString).toLocal();
    final now = DateTime.now();
    final diff = now.difference(dt);
    if (diff.inMinutes < 1) return 'now';
    if (diff.inHours < 24 && dt.day == now.day) {
      return DateFormat('HH:mm').format(dt);
    }
    if (diff.inDays < 7) {
      return DateFormat('EEE', 'en').format(dt);
    }
    return DateFormat('MMM d').format(dt);
  }

  /// Format a timestamp for message bubbles.
  static String messageTime(String isoString) {
    final dt = DateTime.parse(isoString).toLocal();
    return DateFormat('HH:mm').format(dt);
  }

  /// Format a full date for detail screens.
  static String fullDate(String isoString) {
    final dt = DateTime.parse(isoString).toLocal();
    return DateFormat('MMM d, y · HH:mm').format(dt);
  }

  /// Format USD currency.
  static String usd(double amount) {
    return '\$${amount.toStringAsFixed(2)}';
  }

  /// Format a number with thousands separators.
  static String compact(int n) {
    return NumberFormat.compact().format(n);
  }

  /// Format a percentage.
  static String percent(double value, {int decimals = 1}) {
    return '${(value * 100).toStringAsFixed(decimals)}%';
  }

  /// Get initials from a name.
  static String initials(String? name, String fallback) {
    if (name == null || name.trim().isEmpty) return fallback;
    final parts = name.trim().split(' ');
    if (parts.length >= 2) {
      return '${parts[0][0]}${parts[1][0]}'.toUpperCase();
    }
    return name.substring(0, 1).toUpperCase();
  }
}
