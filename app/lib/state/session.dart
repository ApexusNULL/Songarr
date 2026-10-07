import 'dart:io';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../api/client.dart';

class Session {
  Session({required this.server, required this.token, required this.userId, required this.userName, this.servers = const []});
  final String server;
  final String token;
  final int userId;
  final String userName;

  /// Every server's address when there's a backup server (the server says), this one included.
  final List<String> servers;

  Session copyWith({String? server, List<String>? servers}) =>
      Session(server: server ?? this.server, token: token, userId: userId, userName: userName, servers: servers ?? this.servers);
}

const _storage = FlutterSecureStorage();

/// The signed-in server and device token, kept in the OS keystore (Android Keystore etc.).
class SessionNotifier extends AsyncNotifier<Session?> {
  @override
  Future<Session?> build() async {
    final values = await _storage.readAll();
    final server = values['server'], token = values['token'];
    if (server == null || token == null) return null;
    return Session(
      server: server,
      token: token,
      userId: int.tryParse(values['user_id'] ?? '') ?? 0,
      userName: values['user_name'] ?? '',
      servers: [for (final s in (values['servers'] ?? '').split('\n')) if (s.isNotEmpty) s],
    );
  }

  static String deviceName() {
    if (Platform.isAndroid) return 'Android phone';
    if (Platform.isIOS) return 'iPhone';
    if (Platform.isWindows) return 'Windows PC';
    if (Platform.isLinux) return 'Linux PC';
    if (Platform.isMacOS) return 'Mac';
    return 'Songarr app';
  }

  Future<void> pair(String server, String code) async => _signedIn(server, await SongarrApi.pair(server, code, deviceName()));

  Future<void> signIn(String server, String login, String password) async =>
      _signedIn(server, await SongarrApi.signIn(server, login, password, deviceName()));

  /// The server this device last signed in to (kept after signing out, to fill in the form).
  static Future<String?> lastServer() async {
    try {
      return await _storage.read(key: 'last_server');
    } catch (_) {
      return null;
    }
  }

  Future<void> _signedIn(String server, PairResult result) async {
    await _storage.write(key: 'server', value: server);
    await _storage.write(key: 'last_server', value: server);
    await _storage.write(key: 'token', value: result.token);
    await _storage.write(key: 'user_id', value: result.userId.toString());
    await _storage.write(key: 'user_name', value: result.userName);
    state = AsyncData(Session(server: server, token: result.token, userId: result.userId, userName: result.userName));
  }

  /// Another of the family's servers answered (the one in use went away): use it from now on.
  Future<void> useServer(String server) async {
    final s = state.value;
    if (s == null || s.server == server) return;
    state = AsyncData(s.copyWith(server: server)); // at once: whatever runs next uses this server
    await _storage.write(key: 'server', value: server);
    await _storage.write(key: 'last_server', value: server);
  }

  /// Every server's address, from the server (with a backup server, the app can switch).
  Future<void> learnServers(List<String> servers) async {
    final s = state.value;
    if (s == null || servers.join('\n') == s.servers.join('\n')) return;
    await _storage.write(key: 'servers', value: servers.join('\n'));
    state = AsyncData(s.copyWith(servers: servers));
  }

  /// Forget this device. [revoke] also signs the token out on the server.
  Future<void> signOut({bool revoke = true}) async {
    final s = state.value;
    if (revoke && s != null) {
      try {
        await SongarrApi(server: s.server, token: s.token).logout();
      } catch (_) {
        // Offline or already revoked: forgetting it locally is what matters.
      }
    }
    await _storage.deleteAll();
    if (s != null) await _storage.write(key: 'last_server', value: s.server);
    state = const AsyncData(null);
  }
}

final sessionProvider = AsyncNotifierProvider<SessionNotifier, Session?>(SessionNotifier.new);

final apiProvider = Provider<SongarrApi?>((ref) {
  final s = ref.watch(sessionProvider).value;
  if (s == null) return null;
  return SongarrApi(
    server: s.server,
    token: s.token,
    alternatives: [for (final a in s.servers) if (a != s.server) a],
    onSwitch: (other) => ref.read(sessionProvider.notifier).useServer(other),
  );
});
