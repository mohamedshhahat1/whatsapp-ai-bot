/// Base failure class for the app.
sealed class Failure {
  final String message;
  final int? statusCode;
  const Failure({required this.message, this.statusCode});
}

class NetworkFailure extends Failure {
  const NetworkFailure({required super.message, super.statusCode});
}

class ServerFailure extends Failure {
  const ServerFailure({required super.message, super.statusCode});
}

class UnauthorizedFailure extends Failure {
  const UnauthorizedFailure({super.message = 'Session expired', super.statusCode = 401});
}

class TimeoutFailure extends Failure {
  const TimeoutFailure({super.message = 'Request timed out'});
}

class OfflineFailure extends Failure {
  const OfflineFailure({super.message = 'No internet connection'});
}

class NotFoundFailure extends Failure {
  const NotFoundFailure({super.message = 'Not found', super.statusCode = 404});
}

class ValidationFailure extends Failure {
  final List<String> errors;

  /// Not const: the message is built by joining [errors], and a const
  /// constructor initialiser cannot invoke a method.
  ValidationFailure({required this.errors}) : super(message: errors.join(', '), statusCode: 422);
}

class UnknownFailure extends Failure {
  const UnknownFailure({super.message = 'Something went wrong'});
}

/// Convert a DioException to a typed Failure.
Failure dioToFailure(dynamic e) {
  if (e is Failure) return e;
  final message = e.toString();
  return UnknownFailure(message: message);
}
