import 'package:flutter/material.dart';

class AppColors {
  AppColors._();

  // Brand — WhatsApp-inspired teal-green
  static const primary = Color(0xFF00A884);
  static const primaryDark = Color(0xFF008069);
  static const primaryLight = Color(0xFF25D366);

  // Accent — for sales lead gold
  static const gold = Color(0xFFFFB800);
  static const goldGlow = Color(0x33FFB800);

  // Bot mode
  static const botMode = Color(0xFF6366F1);  // Indigo
  static const humanMode = Color(0xFFEC4899); // Pink

  // Semantic
  static const success = Color(0xFF22C55E);
  static const warning = Color(0xFFF59E0B);
  static const error = Color(0xFFEF4444);
  static const info = Color(0xFF3B82F6);

  // Light theme surfaces
  static const lightBg = Color(0xFFF7F8FA);
  static const lightSurface = Color(0xFFFFFFFF);
  static const lightChatBg = Color(0xFFECE5DD); // WhatsApp chat bg

  // Dark theme surfaces
  static const darkBg = Color(0xFF0B1215);
  static const darkSurface = Color(0xFF111B1E);
  static const darkChatBg = Color(0xFF0B141A); // WhatsApp dark chat bg

  // Outbound bubble (light)
  static const outBubbleLight = Color(0xFFD9FDD3); // WhatsApp light green
  // Inbound bubble (light)
  static const inBubbleLight = Color(0xFFFFFFFF);
  // Outbound bubble (dark)
  static const outBubbleDark = Color(0xFF005C4B);
  // Inbound bubble (dark)
  static const inBubbleDark = Color(0xFF202C33);

  // Glass
  static const glassLight = Color(0xCCFFFFFF);
  static const glassDark = Color(0xCC111B1E);
}
