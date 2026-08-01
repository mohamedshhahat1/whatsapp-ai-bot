import 'package:flutter/material.dart';

class AppLocalizations {
  final Locale locale;
  AppLocalizations(this.locale);

  static const _localizedValues = {
    'en': {
      'chats': 'Chats', 'customers': 'Customers', 'analytics': 'Analytics', 'notifications': 'Notifications', 'settings': 'Settings',
      'login': 'Sign In', 'logout': 'Logout', 'search': 'Search', 'send': 'Send', 'takeOver': 'Take Over', 'resumeAi': 'Resume AI',
      'botMode': 'Bot Mode', 'humanMode': 'Human Mode', 'salesLead': 'Sales Lead', 'darkMode': 'Dark Mode', 'language': 'Language',
      'about': 'About', 'privacy': 'Privacy', 'noConversations': 'No conversations', 'noCustomers': 'No customers yet',
      'noNotifications': 'No notifications', 'retry': 'Retry', 'delete': 'Delete', 'cancel': 'Cancel', 'confirmDelete': 'Delete conversation?',
      'confirmLogout': 'Logout?', 'typeMessage': 'Type a message...', 'serverUrl': 'Server URL', 'apiKey': 'API Key',
      'operatorName': 'Your Name (optional)', 'totalUsers': 'Total Users', 'conversations': 'Conversations', 'messages': 'Messages',
      'aiRequests': 'AI Requests', 'costBreakdown': 'Cost Breakdown', 'systemHealth': 'System Health', 'dailyActivity': 'Daily Activity',
      'topQuestions': 'Top Questions', 'costByModel': 'Cost by Model', 'unblockCustomer': 'Unblock Customer',
    },
    'ar': {
      'chats': 'المحادثات', 'customers': 'العملاء', 'analytics': 'التحليلات', 'notifications': 'الإشعارات', 'settings': 'الإعدادات',
      'login': 'تسجيل الدخول', 'logout': 'تسجيل الخروج', 'search': 'بحث', 'send': 'إرسال', 'takeOver': 'تولي المحادثة',
      'resumeAi': 'استئناف الذكاء الاصطناعي', 'botMode': 'وضع البوت', 'humanMode': 'وضع الإنسان', 'salesLead': 'عميل محتمل',
      'darkMode': 'الوضع الداكن', 'language': 'اللغة', 'about': 'حول التطبيق', 'privacy': 'الخصوصية',
      'noConversations': 'لا توجد محادثات', 'noCustomers': 'لا يوجد عملاء بعد', 'noNotifications': 'لا توجد إشعارات',
      'retry': 'إعادة المحاولة', 'delete': 'حذف', 'cancel': 'إلغاء', 'confirmDelete': 'حذف المحادثة؟', 'confirmLogout': 'تسجيل الخروج؟',
      'typeMessage': 'اكتب رسالة...', 'serverUrl': 'رابط الخادم', 'apiKey': 'مفتاح API', 'operatorName': 'اسمك (اختياري)',
      'totalUsers': 'إجمالي المستخدمين', 'conversations': 'المحادثات', 'messages': 'الرسائل', 'aiRequests': 'طلبات الذكاء الاصطناعي',
      'costBreakdown': 'تفاصيل التكلفة', 'systemHealth': 'صحة النظام', 'dailyActivity': 'النشاط اليومي',
      'topQuestions': 'الأسئلة الأكثر شيوعاً', 'costByModel': 'التكلفة حسب النموذج', 'unblockCustomer': 'إلغاء حظر العميل',
    },
  };

  static const _supportedLocales = [Locale('en'), Locale('ar')];
  static const LocalizationsDelegate<AppLocalizations> delegate = _AppLocalizationsDelegate();
  static List<Locale> get supportedLocales => _supportedLocales;

  String get(String key) => _localizedValues[locale.languageCode]?[key] ?? _localizedValues['en']![key] ?? key;
  String get chats => get('chats');
  String get customers => get('customers');
  String get analytics => get('analytics');
  String get notifications => get('notifications');
  String get settings => get('settings');
  String get login => get('login');
  String get logout => get('logout');
  String get search => get('search');
  String get send => get('send');
  String get takeOver => get('takeOver');
  String get resumeAi => get('resumeAi');
  String get botMode => get('botMode');
  String get humanMode => get('humanMode');
  String get salesLead => get('salesLead');
  String get darkMode => get('darkMode');
  String get language => get('language');
  String get about => get('about');
  String get privacy => get('privacy');
  String get typeMessage => get('typeMessage');
  String get serverUrl => get('serverUrl');
  String get apiKey => get('apiKey');
  String get operatorName => get('operatorName');
  String get unblockCustomer => get('unblockCustomer');
}

class _AppLocalizationsDelegate extends LocalizationsDelegate<AppLocalizations> {
  const _AppLocalizationsDelegate();
  @override
  bool isSupported(Locale locale) => ['en', 'ar'].contains(locale.languageCode);
  @override
  Future<AppLocalizations> load(Locale locale) async => AppLocalizations(locale);
  @override
  bool shouldReload(_AppLocalizationsDelegate old) => false;
}
