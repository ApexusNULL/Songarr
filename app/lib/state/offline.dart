import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/client.dart';
import '../api/models.dart';
import 'listen_scores.dart';
import 'session.dart';

/// Songs on this device.
///
/// * **Downloads** are songs you chose to keep offline. They're never removed automatically.
/// * **Cache** holds songs as they're streamed, so replays (and offline listening) need no
///   network. Once it passes its size limit, the songs you've listened to least over time
///   are removed first (see [ListenScores]).
///
/// * **Podcast episodes** mirror what the server keeps for this profile (until played, or for
///   some days) and are deleted here when the server lets them go.
///
/// Files are stored exactly as the server sends them: the original AAC/Opus file is already
/// compressed, so it's kept byte-for-byte and decoded only during playback.
class OfflineState {
  const OfflineState({
    this.downloads = const {},
    this.episodes = const {},
    this.progress = const {},
    this.cacheBytes = 0,
    this.cacheLimit = defaultCacheLimit,
  });

  static const defaultCacheLimit = 2 * 1024 * 1024 * 1024; // 2 GB

  final Map<String, Track> downloads;
  final Map<String, Episode> episodes;
  final Map<String, double> progress; // track or episode id -> 0..1 while downloading
  final int cacheBytes;
  final int cacheLimit;

  int get downloadBytes => downloads.values.fold(0, (sum, t) => sum + (t.size ?? 0));
  int get episodeBytes => episodes.values.fold(0, (sum, e) => sum + (e.size ?? 0));

  OfflineState copyWith(
          {Map<String, Track>? downloads, Map<String, Episode>? episodes, Map<String, double>? progress, int? cacheBytes, int? cacheLimit}) =>
      OfflineState(
        downloads: downloads ?? this.downloads,
        episodes: episodes ?? this.episodes,
        progress: progress ?? this.progress,
        cacheBytes: cacheBytes ?? this.cacheBytes,
        cacheLimit: cacheLimit ?? this.cacheLimit,
      );
}

class OfflineStore extends Notifier<OfflineState> {
  late Directory _downloadsDir;
  late Directory _episodesDir;
  late Directory _cacheDir;
  bool _syncing = false;
  late File _scoresFile;
  final scores = ListenScores();

  /// The song playing right now is never pruned.
  String? protectedId;

  /// The next few songs in the queue, fetched ahead into the cache (see [fetchAhead]); kept too.
  Set<String> upcoming = {};
  final _fetching = <String>{};
  final _ready = Completer<void>();
  final _queue = <Track>[];
  int _running = 0;
  static const _parallel = 2;

  @override
  OfflineState build() {
    _init();
    return const OfflineState();
  }

  Future<void> get ready => _ready.future;

  Future<void> _init() async {
    final support = await getApplicationSupportDirectory();
    final cache = await getApplicationCacheDirectory();
    _downloadsDir = Directory('${support.path}/downloads')..createSync(recursive: true);
    _episodesDir = Directory('${support.path}/episodes')..createSync(recursive: true);
    _cacheDir = Directory('${cache.path}/stream-cache')..createSync(recursive: true);
    _scoresFile = File('${support.path}/listen_scores.json');
    if (_scoresFile.existsSync()) {
      try {
        scores.loadJson(jsonDecode(_scoresFile.readAsStringSync()) as Map<String, dynamic>);
      } catch (_) {
        // Lost scores only make pruning fall back to "least recently played".
      }
    }
    final prefs = await SharedPreferences.getInstance();
    final limit = prefs.getInt('cache_limit') ?? OfflineState.defaultCacheLimit;
    final downloads = <String, Track>{};
    final index = File('${_downloadsDir.path}/index.json');
    if (index.existsSync()) {
      try {
        for (final j in jsonDecode(index.readAsStringSync()) as List) {
          final t = Track.fromJson(j as Map<String, dynamic>);
          if (File(_downloadPath(t)).existsSync()) downloads[t.id] = t;
        }
      } catch (_) {
        // A damaged index only loses the list; the files are re-indexed when downloaded again.
      }
    }
    final episodes = <String, Episode>{};
    final epIndex = File('${_episodesDir.path}/index.json');
    if (epIndex.existsSync()) {
      try {
        for (final j in jsonDecode(epIndex.readAsStringSync()) as List) {
          final e = Episode(j as Map<String, dynamic>);
          if (File(_episodePath(e.id)).existsSync()) episodes[e.id] = e;
        }
      } catch (_) {
        // re-synced from the server next time
      }
    }
    state = state.copyWith(downloads: downloads, episodes: episodes, cacheLimit: limit, cacheBytes: _cacheSize());
    if (!_ready.isCompleted) _ready.complete();
    await trimCache();
  }

  String _downloadPath(Track t) => '${_downloadsDir.path}/${t.id}.${t.format}';
  String _episodePath(String id) => '${_episodesDir.path}/$id';

  /// A podcast episode kept on this device, if it is.
  String? episodeFile(String id) {
    if (!state.episodes.containsKey(id)) return null;
    final p = _episodePath(id);
    return File(p).existsSync() ? p : null;
  }

  void _saveEpisodeIndex() =>
      File('${_episodesDir.path}/index.json').writeAsStringSync(jsonEncode([for (final e in state.episodes.values) e.json]));

  /// Make this device hold the episodes the server keeps for this profile: fetch new ones
  /// (once the server has them) and delete ones that were played or ran out of days.
  Future<void> syncEpisodes() async {
    await ready;
    final api = ref.read(apiProvider);
    if (api == null || _syncing) return;
    _syncing = true;
    try {
      final wanted = await api.podcastDownloads();
      final ids = {for (final e in wanted) e.id};
      final kept = <String, Episode>{};
      for (final e in state.episodes.values) {
        if (ids.contains(e.id)) {
          kept[e.id] = e;
        } else {
          try {
            File(_episodePath(e.id)).deleteSync();
          } catch (_) {}
        }
      }
      for (final e in wanted) {
        if (kept.containsKey(e.id)) kept[e.id] = Episode({...e.json, 'size': kept[e.id]!.size}); // keep/expiry may change
      }
      state = state.copyWith(episodes: kept);
      _saveEpisodeIndex();
      for (final e in wanted.where((e) => e.onServer && !kept.containsKey(e.id))) {
        await _downloadEpisode(api, e);
      }
    } catch (_) {
      // offline or signed out: try again next time
    } finally {
      _syncing = false;
    }
  }

  /// The server needs a moment to fetch new episodes; look again a couple of times.
  void syncEpisodesSoon() {
    for (final s in const [3, 20, 60]) {
      Future.delayed(Duration(seconds: s), syncEpisodes);
    }
  }

  Future<void> _downloadEpisode(SongarrApi api, Episode e) async {
    final path = _episodePath(e.id);
    final partial = '$path.part';
    state = state.copyWith(progress: {...state.progress, e.id: 0});
    try {
      await Dio().download(
        api.episodeStreamUri(e.id).toString(),
        partial,
        options: Options(headers: api.authHeaders),
        onReceiveProgress: (got, total) {
          if (total > 0) state = state.copyWith(progress: {...state.progress, e.id: got / total});
        },
      );
      await File(partial).rename(path);
      state = state.copyWith(episodes: {...state.episodes, e.id: Episode({...e.json, 'size': File(path).lengthSync()})});
      _saveEpisodeIndex();
    } catch (_) {
      try {
        File(partial).deleteSync();
      } catch (_) {}
    } finally {
      state = state.copyWith(progress: {...state.progress}..remove(e.id));
    }
  }
  String cachePath(Track t) => '${_cacheDir.path}/${t.id}.${t.format}';

  /// Fetch [tracks] (the songs coming up next) into the listening cache, one at a time, so the
  /// phone has music in hand: if the server goes away (a backup server takes about a minute to
  /// take over), playback carries on from the phone meanwhile. The player picks a cached song up
  /// from the phone even when it was queued as a stream.
  Future<void> fetchAhead(SongarrApi api, List<Track> tracks) async {
    await ready;
    upcoming = {for (final t in tracks) t.id};
    for (final t in tracks) {
      if (!upcoming.contains(t.id)) return; // the queue moved on meanwhile
      if (t.isEpisode || t.isPreview || !t.playable || localFile(t) != null || _fetching.contains(t.id)) continue;
      if (File('${cachePath(t)}.part').existsSync()) continue; // the player is streaming it right now
      _fetching.add(t.id);
      final partial = '${cachePath(t)}.ahead';
      try {
        final r = await Dio().download(api.streamUri(t.id).toString(), partial, options: Options(headers: api.authHeaders));
        if (File(cachePath(t)).existsSync()) {
          File(partial).deleteSync(); // the player cached it first
        } else {
          await File(partial).rename(cachePath(t));
          final type = r.headers.value('content-type');
          if (type != null) File('${cachePath(t)}.mime').writeAsStringSync(type);
        }
      } catch (_) {
        try {
          File(partial).deleteSync();
        } catch (_) {}
        return; // offline or the server is away: try again with the next song change
      } finally {
        _fetching.remove(t.id);
      }
    }
  }

  /// The local file to play for [t], if there is one (a download, or a fully cached stream).
  String? localFile(Track t) {
    final d = state.downloads[t.id];
    if (d != null) {
      final p = _downloadPath(d);
      if (File(p).existsSync()) return p;
    }
    final c = File(cachePath(t));
    if (c.existsSync()) {
      c.setLastModifiedSync(DateTime.now()); // tie-breaker for songs with equal listening scores
      return c.path;
    }
    return null;
  }

  /// Count a listen of [id] towards its score (see [ListenScores.weightFor]).
  Future<void> recordListen(String id, Duration played, Duration length) async {
    await ready;
    final w = ListenScores.weightFor(played, length);
    if (w <= 0) return;
    scores
      ..add(id, w)
      ..compact();
    try {
      _scoresFile.writeAsStringSync(jsonEncode(scores.toJson()));
    } catch (_) {}
  }

  bool isDownloaded(String id) => state.downloads.containsKey(id);

  void _saveIndex() {
    File('${_downloadsDir.path}/index.json')
        .writeAsStringSync(jsonEncode([for (final t in state.downloads.values) t.json]));
  }

  /// Keep [tracks] on this device. Songs the server hasn't downloaded yet are skipped.
  Future<int> download(List<Track> tracks) async {
    await ready;
    final api = ref.read(apiProvider);
    if (api == null) return 0;
    final todo = tracks.where((t) => t.playable && !isDownloaded(t.id) && !state.progress.containsKey(t.id)).toList();
    if (todo.isEmpty) return 0;
    state = state.copyWith(progress: {...state.progress, for (final t in todo) t.id: 0});
    _queue.addAll(todo);
    _pump(api);
    return todo.length;
  }

  void _pump(SongarrApi api) {
    while (_running < _parallel && _queue.isNotEmpty) {
      final t = _queue.removeAt(0);
      _running++;
      _downloadOne(api, t).whenComplete(() {
        _running--;
        _pump(api);
      });
    }
  }

  Future<void> _downloadOne(SongarrApi api, Track t) async {
    final path = _downloadPath(t);
    final partial = '$path.part';
    try {
      final cached = File(cachePath(t));
      if (cached.existsSync()) {
        await cached.copy(partial); // already streamed once: no need to fetch it again
      } else {
        await Dio().download(
          api.streamUri(t.id).toString(),
          partial,
          options: Options(headers: api.authHeaders),
          onReceiveProgress: (got, total) {
            if (total > 0) state = state.copyWith(progress: {...state.progress, t.id: got / total});
          },
        );
      }
      await File(partial).rename(path);
      final size = File(path).lengthSync();
      state = state.copyWith(downloads: {...state.downloads, t.id: Track({...t.json, 'size': size})});
      _saveIndex();
    } catch (_) {
      try {
        File(partial).deleteSync();
      } catch (_) {}
    } finally {
      state = state.copyWith(progress: {...state.progress}..remove(t.id));
    }
  }

  Future<void> remove(List<String> ids) async {
    await ready;
    final downloads = {...state.downloads};
    for (final id in ids) {
      final t = downloads.remove(id);
      if (t != null) {
        try {
          File(_downloadPath(t)).deleteSync();
        } catch (_) {}
      }
    }
    state = state.copyWith(downloads: downloads);
    _saveIndex();
  }

  int _cacheSize() => _cacheDir
      .listSync()
      .whereType<File>()
      .fold(0, (sum, f) => sum + f.lengthSync());

  /// Track id of a cache file: `<id>.<ext>`, plus just_audio's `.part` / `.mime` side files.
  static String _idOf(File f) => f.uri.pathSegments.last.split('.').first;

  /// Remove cached songs, least listened over time first, until the cache fits its limit.
  Future<void> trimCache() async {
    await ready;
    final byId = <String, List<File>>{};
    for (final f in _cacheDir.listSync().whereType<File>()) {
      byId.putIfAbsent(_idOf(f), () => []).add(f);
    }
    int sizeOf(String id) => byId[id]!.fold(0, (s, f) => s + f.lengthSync());
    DateTime lastUsed(String id) =>
        byId[id]!.map((f) => f.lastModifiedSync()).reduce((a, b) => a.isAfter(b) ? a : b);
    var total = byId.keys.fold<int>(0, (s, id) => s + sizeOf(id));
    final candidates = byId.keys.where((id) {
      if (id == protectedId || upcoming.contains(id)) return false;
      final streaming = byId[id]!.any((f) => f.path.endsWith('.part') || f.path.endsWith('.ahead'));
      return !(streaming && DateTime.now().difference(lastUsed(id)).inMinutes < 10); // still being cached
    });
    for (final id in scores.pruneOrder(candidates, lastUsed)) {
      if (total <= state.cacheLimit) break;
      total -= sizeOf(id);
      for (final f in byId[id]!) {
        try {
          f.deleteSync();
        } catch (_) {}
      }
    }
    state = state.copyWith(cacheBytes: total);
  }

  Future<void> setCacheLimit(int bytes) async {
    (await SharedPreferences.getInstance()).setInt('cache_limit', bytes);
    state = state.copyWith(cacheLimit: bytes);
    await trimCache();
  }

  Future<void> clearCache() async {
    await ready;
    for (final f in _cacheDir.listSync().whereType<File>()) {
      try {
        f.deleteSync();
      } catch (_) {}
    }
    state = state.copyWith(cacheBytes: 0);
  }

  /// Called when the account changes: downloads belong to whoever was signed in.
  Future<void> wipe() async {
    await ready;
    await remove(state.downloads.keys.toList());
    for (final id in state.episodes.keys) {
      try {
        File(_episodePath(id)).deleteSync();
      } catch (_) {}
    }
    state = state.copyWith(episodes: {});
    _saveEpisodeIndex();
    await clearCache();
  }
}

final offlineProvider = NotifierProvider<OfflineStore, OfflineState>(OfflineStore.new);
