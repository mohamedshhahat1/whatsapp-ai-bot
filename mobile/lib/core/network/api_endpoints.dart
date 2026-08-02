/// All backend admin API endpoints, mapped from the FastAPI router.
class ApiEndpoints {
  ApiEndpoints._();

  // Users
  static String users({int offset = 0, int limit = 50}) =>
      '/users?offset=$offset&limit=$limit';

  // Conversations
  //
  // A conversation is one SESSION, not one customer: sessions close after a
  // period of silence and a returning customer opens a new one, so the same
  // person appears several times in this list over time.
  //
  // [status] is optional ('active' or 'closed'); omitting it returns every
  // session, which is the behaviour this endpoint has always had.
  static String conversations({
    int offset = 0,
    int limit = 50,
    String? status,
  }) {
    final query = '/conversations?offset=$offset&limit=$limit';
    if (status == null || status.isEmpty) return query;
    return '$query&status=${Uri.encodeQueryComponent(status)}';
  }

  static String conversation(int id) => '/conversations/$id';

  /// The customer behind a session and their earlier ones. Sessions are never
  /// merged, so this returns navigation between them rather than a combined
  /// transcript.
  static String conversationHistory(int id, {int limit = 20}) =>
      '/conversations/$id/history?limit=$limit';

  static String takeOver(int id) => '/conversations/$id/takeover';
  static String resumeAi(int id) => '/conversations/$id/resume-ai';
  static String reply(int id) => '/conversations/$id/reply';

  // Stats
  static String stats() => '/stats';

  // Search
  static String search(String q, {int limit = 50}) =>
      '/search?q=${Uri.encodeQueryComponent(q)}&limit=$limit';

  // Analytics
  static String overview({int days = 30}) => '/analytics/overview?days=$days';
  static String daily({int days = 30}) => '/analytics/daily?days=$days';
  static String models({int days = 30}) => '/analytics/models?days=$days';
  static String questions({int days = 30, int limit = 10}) =>
      '/analytics/questions?days=$days&limit=$limit';
  static String customers({int offset = 0, int limit = 50}) =>
      '/analytics/customers?offset=$offset&limit=$limit';

  // Quota
  static String quota() => '/quota';
  static String aiToggle() => '/ai-toggle';
  static String unblock(String waId) =>
      '/customers/${Uri.encodeQueryComponent(waId)}/unblock';

  // Knowledge
  static String knowledge() => '/knowledge';
  static String knowledgeSearch(String q, {int limit = 5}) =>
      '/knowledge/search?q=${Uri.encodeQueryComponent(q)}&limit=$limit';
}
