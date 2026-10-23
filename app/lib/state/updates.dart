import 'dart:io';

import 'package:dio/dio.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';

import '../api/client.dart';
import '../api/models.dart';
import 'session.dart';

class UpdateState {
  const UpdateState({this.version = '', this.build = 0, this.update, this.progress, this.apk, this.checking = false, this.error});
  final String version; // this app's version, e.g. 1.1.0
  final int build;
  final AppUpdate? update;
  final double? progress; // 0..1 while downloading
  final String? apk; // downloaded and ready to install
  final bool checking;
  final String? error;

  bool get ready => update != null && apk != null;

  UpdateState copyWith({String? version, int? build, AppUpdate? update, bool clearUpdate = false, double? progress, bool clearProgress = false,
          String? apk, bool clearApk = false, bool? checking, String? error, bool clearError = false}) =>
      UpdateState(
        version: version ?? this.version,
        build: build ?? this.build,
        update: clearUpdate ? null : update ?? this.update,
        progress: clearProgress ? null : progress ?? this.progress,
        apk: clearApk ? null : apk ?? this.apk,
        checking: checking ?? this.checking,
        error: clearError ? null : error ?? this.error,
      );
}

/// Asks the Songarr server for a newer app, fetches it in the background, and hands it to
/// Android's installer when you tap Install (Android checks it's signed like this one). On Windows
/// it's the desktop installer, which runs by itself and opens the app again.
class Updater extends Notifier<UpdateState> {
  static const _channel = MethodChannel('songarr/update');
  // The desktop app's version, given by its build (app/windows/installer/build.py); 0 when run by hand.
  static const _desktopBuild = int.fromEnvironment('APP_BUILD');
  static const _desktopVersion = String.fromEnvironment('APP_VERSION');
  static bool get supported => Platform.isAndroid || (Platform.isWindows && _desktopBuild > 0);
  DateTime? _lastCheck;
  String _abi = '';

  @override
  UpdateState build() {
    ref.watch(apiProvider); // signed in again (or elsewhere): forget the last check and its error
    _lastCheck = null;
    return const UpdateState();
  }

  Future<void> check({bool force = false}) async {
    if (!supported || state.checking) return;
    if (!force && _lastCheck != null && DateTime.now().difference(_lastCheck!) < const Duration(hours: 1)) return;
    final api = ref.read(apiProvider);
    if (api == null) return;
    _lastCheck = DateTime.now();
    state = state.copyWith(checking: true, clearError: true);
    try {
      final int build;
      if (Platform.isWindows) {
        _abi = 'windows-x64';
        build = _desktopBuild;
        state = state.copyWith(version: _desktopVersion, build: build);
      } else {
        final info = await _channel.invokeMapMethod<String, dynamic>('info') ?? const {};
        final code = (info['versionCode'] as num?)?.toInt() ?? 0;
        _abi = info['abi'] as String? ?? '';
        // Split-per-ABI builds number themselves processor * 1000 + build.
        build = code >= 1000 ? code % 1000 : code;
        state = state.copyWith(version: info['versionName'] as String? ?? '', build: build);
      }
      final found = await api.appUpdate(_abi, build);
      if (found == null) {
        state = state.copyWith(clearUpdate: true, clearApk: true, clearProgress: true);
        return;
      }
      state = state.copyWith(update: found);
      await _download(api, found);
    } on ApiException catch (e) {
      if (Platform.isWindows && e.status == 400) {
        // a server from before the desktop app: it has nothing newer for it
        state = state.copyWith(clearUpdate: true, clearApk: true, clearProgress: true);
      } else {
        state = state.copyWith(error: e.message);
      }
    } catch (e) {
      state = state.copyWith(error: 'Couldn\'t check for updates ($e)');
    } finally {
      state = state.copyWith(checking: false);
    }
  }

  Future<void> _download(SongarrApi api, AppUpdate u) async {
    final dir = Directory('${(await getApplicationCacheDirectory()).path}/updates')..createSync(recursive: true);
    final path = '${dir.path}/songarr-${u.build}-${u.abi}.${Platform.isWindows ? 'exe' : 'apk'}';
    for (final f in dir.listSync().whereType<File>()) {
      if (f.path != path) f.deleteSync(); // older downloads
    }
    final file = File(path);
    if (!file.existsSync() || file.lengthSync() != u.size) {
      state = state.copyWith(progress: 0);
      await Dio().download(
        api.appUpdateUri(u.abi).toString(),
        '$path.part',
        options: Options(headers: api.authHeaders),
        onReceiveProgress: (got, total) {
          if (total > 0) state = state.copyWith(progress: got / total);
        },
      );
      await File('$path.part').rename(path);
    }
    state = state.copyWith(apk: path, clearProgress: true);
  }

  /// Returns a message for the user.
  Future<String?> install() async {
    final u = state.update, apk = state.apk;
    if (u == null || apk == null) return 'No update is ready yet.';
    if (Platform.isWindows) return _installOnWindows(u, apk);
    try {
      final r = await _channel.invokeMethod<String>('install', {'path': apk, 'sha256': u.sha256});
      if (r == 'needs-permission') {
        return 'Allow ${Brand.current} to install updates, then come back and tap Install again.';
      }
      return 'Installing… ${Brand.current} closes for a moment; tap the notification to reopen it.';
    } on PlatformException catch (e) {
      state = state.copyWith(clearApk: true);
      _lastCheck = null; // a damaged download is fetched again on the next check
      return e.message ?? 'The update couldn\'t be installed.';
    }
  }

  /// The desktop installer, checked against what the server says it is, runs quietly and opens the
  /// app again; this copy closes for it.
  Future<String?> _installOnWindows(AppUpdate u, String path) async {
    try {
      final hash = await Process.run('certutil', ['-hashfile', path, 'SHA256']);
      final lines = '${hash.stdout}'.split(RegExp(r'\r?\n')).map((l) => l.replaceAll(' ', '').toLowerCase());
      if (u.sha256.isNotEmpty && !lines.contains(u.sha256.toLowerCase())) {
        File(path).deleteSync();
        state = state.copyWith(clearApk: true);
        _lastCheck = null;
        return 'The download didn\'t come through whole. It\'s fetched again on the next check.';
      }
      await Process.start(path, ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS'],
          mode: ProcessStartMode.detached);
      exit(0);
    } on ProcessException catch (e) {
      return 'The update couldn\'t be started (${e.message}).';
    }
  }
}

final updaterProvider = NotifierProvider<Updater, UpdateState>(Updater.new);
