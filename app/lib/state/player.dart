import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:flutter/foundation.dart';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:just_audio/just_audio.dart';
import 'package:audio_service/audio_service.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/client.dart';
import '../api/models.dart';
import 'audio_handler.dart';
import 'data.dart';
import 'offline.dart';
import 'session.dart';

/// What the player tells a Jam it's in: the user's own actions, which everyone should hear.
abstract class JamLink {
  Future<void> setPlaying(bool playing);
  Future<void> seekTo(Duration position);
  Future<void> skip(int delta);
  Future<void> jumpTo(int index);
  Future<void> replace(List<Track> tracks, int index);
  Future<void> add(List<Track> tracks, {bool next = false});
}

/// Playback: one queue, background audio with lock-screen controls, and listen reporting.
///
/// Each song plays from the best source available: a download on this device, then a
/// cached copy from an earlier play, then a stream from the server that is cached as it plays.
/// Podcast episodes stream through the server and remember where you stopped.
class PlayerController {
  PlayerController(this.ref, this.handler) {
    // Headphones, the notification and the lock screen skip the way the app does.
    handler.onSkipToNext = next;
    handler.onSkipToPrevious = previous;
    handler.onPlay = () => jam != null ? jam!.setPlaying(true) : player.play();
    handler.onPause = () => jam != null ? jam!.setPlaying(false) : player.pause();
    handler.onSeek = seek;
    SharedPreferences.getInstance().then((p) => _podcastSpeed = p.getDouble('podcast_speed') ?? 1.0).catchError((_) => 1.0);
    _subs.addAll([
      player.currentIndexStream.listen(_onIndex),
      player.currentIndexStream.listen((_) => _currentChanges.add(current)),
      player.positionStream.listen((p) {
        if (player.currentIndex != _lastIndex) return;
        _lastPosition = p;
        final t = current;
        if (t != null && t.isEpisode && !_previewing && (p - _lastSaved).abs() > const Duration(seconds: 30)) _saveEpisode(t, p);
        if (player.playing) _saveSessionSoon(); // every few seconds, so even a force-close loses little
      }),
      player.playingStream.listen((playing) {
        final t = current;
        if (!playing && !_previewing && t != null && t.isEpisode) _saveEpisode(t, player.position); // paused: resume here later
        if (!playing) _saveSession();
      }),
      player.playerStateStream.listen((s) {
        if (s.processingState == ProcessingState.completed) {
          final i = _lastIndex;
          if (i != null && i < _queue.length) _report(_queue[i], _queue[i].duration);
          _lastIndex = null;
        }
      }),
      player.errorStream.listen((e) async {
        if (_previewing) return; // the preview sheet says there's no preview
        if (await _tryOtherServer()) return; // its server went away and another took over: carry on there
        if (await _serverAway()) return _waitForServer(); // none answers yet: wait, don't skip through everything
        _messages.add('Couldn\'t play this song (${e.message ?? 'error ${e.code}'}).');
        if (player.hasNext) player.seekToNext();
      }),
    ]);
  }

  final Ref ref;
  /// The media session; it owns the one audio player.
  final SongarrAudioHandler handler;
  AudioPlayer get player => handler.player;
  final _subs = <StreamSubscription>[];
  final _messages = StreamController<String>.broadcast();
  final _queueChanges = StreamController<List<Track>>.broadcast();
  final _currentChanges = StreamController<Track?>.broadcast();

  void _queueChanged() {
    handler.setItems([for (final t in _queue) mediaItemFor(t)]); // what the notification and car show
    _queueChanges.add(queue);
    _currentChanges.add(current);
  }
  List<Track> _queue = const [];

  /// Set while in a Jam: the user's actions go to the Jam (so everyone hears them), and the Jam
  /// drives the player through [applyJam]. The player then holds only the Jam's current item.
  JamLink? jam;
  List<Track>? _jamQueue;
  int _jamIndex = 0;
  bool get inJam => jam != null;

  /// Like state changed since the queue was built (track id -> liked).
  final likes = ValueNotifier<Map<String, bool>>(const {});

  bool isLiked(Track t) => likes.value[t.id] ?? t.liked;
  int? _lastIndex;
  Duration _lastPosition = Duration.zero;
  Duration _lastSaved = Duration.zero;
  double _podcastSpeed = 1.0;

  /// While previewing, what was playing before (put back when the preview ends).
  ({List<Track> queue, int index, Duration position, bool playing, LoopMode loop})? _beforePreview;
  bool _previewing = false;
  bool get previewing => _previewing;

  /// Short notices for the UI (e.g. a song that isn't downloaded to the server yet).
  Stream<String> get messages => _messages.stream;
  void say(String message) => _messages.add(message);
  Stream<List<Track>> get queueChanges => _queueChanges.stream;
  List<Track> get queue => _jamQueue ?? _queue;

  /// Position of [current] in [queue].
  int? get currentIndex => _jamQueue != null ? _jamIndex : player.currentIndex;

  Track? get current {
    final jq = _jamQueue;
    if (jq != null) return _jamIndex < jq.length ? jq[_jamIndex] : null;
    final i = player.currentIndex;
    return i != null && i < _queue.length ? _queue[i] : null;
  }

  /// The song (or episode) playing; also changes when the queue is replaced or cleared.
  Stream<Track?> get currentStream => _currentChanges.stream;

  OfflineStore get _offline => ref.read(offlineProvider.notifier);

  bool canPlay(Track t) => t.playable || _offline.localFile(t) != null;

  /// How a song or episode appears in the notification, on the lock screen and in the car.
  static MediaItem mediaItemFor(Track t) => MediaItem(
        id: t.id,
        title: t.title,
        artist: t.isPreview ? 'Preview · ${t.artistLine}' : t.artistLine,
        album: t.album,
        duration: t.duration == Duration.zero ? null : t.duration,
        artUri: t.coverUrl == null ? null : Uri.parse(t.coverUrl!),
        extras: {'episode': t.isEpisode},
      );

  AudioSource _source(Track t) {
    final tag = mediaItemFor(t);
    final api = ref.read(apiProvider);
    if (t.isPreview) {
      return AudioSource.uri(Uri.parse(t.json['preview_uri'] as String), headers: api?.authHeaders, tag: tag);
    }
    if (t.isEpisode) {
      final kept = _offline.episodeFile(t.id);
      if (kept != null) return AudioSource.file(kept, tag: tag);
      if (api == null) throw StateError('Not signed in');
      return AudioSource.uri(api.episodeStreamUri(t.id), headers: api.authHeaders, tag: tag);
    }
    final local = _offline.localFile(t);
    if (local != null) return AudioSource.file(local, tag: tag);
    if (api == null) throw StateError('Not signed in');
    // Experimental in just_audio, but it's exactly "cache the original file while it streams".
    // ignore: experimental_member_use
    return LockCachingAudioSource(api.streamUri(t.id), headers: api.authHeaders, cacheFile: File(_offline.cachePath(t)), tag: tag);
  }

  bool _switching = false;
  Timer? _retry;

  /// The server stopped answering mid-song. If another of the family's servers is active now, the
  /// queue carries on there from the same spot. False when there's no other server to go to.
  Future<bool> _tryOtherServer() async {
    final api = ref.read(apiProvider);
    if (api == null || _switching || jam != null || api.alternatives.isEmpty || _queue.isEmpty) return false;
    _switching = true;
    try {
      if (await api.findServer() == null) return false;
      await _reloadQueue();
      return true;
    } catch (_) {
      return false;
    } finally {
      _switching = false;
    }
  }

  /// The queue's songs again, from whichever server is in use now, carrying on from the same spot.
  Future<void> _reloadQueue() async {
    final index = player.currentIndex ?? 0, position = player.position, shuffled = player.shuffleModeEnabled;
    await player.setAudioSources([for (final t in _queue) _source(t)], initialIndex: index, initialPosition: position);
    await player.setShuffleModeEnabled(shuffled);
    unawaited(player.play());
  }

  /// No server can be reached at all (not a song that won't play)?
  Future<bool> _serverAway() async {
    final api = ref.read(apiProvider);
    if (api == null || jam != null || _queue.isEmpty) return false;
    try {
      await api.me();
      return false;
    } on ApiException catch (e) {
      return e.away;
    } catch (_) {
      return true;
    }
  }

  /// Keep trying for a few minutes (a backup server takes about one to take over), then carry on.
  void _waitForServer() {
    if (_retry != null) return;
    _messages.add('Can\'t reach your server. Trying again…');
    var tries = 0;
    _retry = Timer.periodic(const Duration(seconds: 10), (t) async {
      if (_switching) return;
      tries++;
      final back = await _tryOtherServer() || !await _serverAway();
      if (back || tries >= 18) {
        t.cancel();
        _retry = null;
        if (back && !player.playing) await _reloadQueue();
        if (back) _messages.add('Back on your server.');
      }
    });
  }

  /// Replace the queue with [tracks] and start at [index] (or a random song when shuffling).
  /// Songs that can't play yet (not downloaded to the server, not on this device) are skipped.
  /// An episode starts where you left off.
  Future<void> play(List<Track> tracks, {int index = 0, bool shuffle = false}) async {
    if (jam != null) {
      // In a Jam, picking music changes it for everyone.
      final usable = [for (final t in tracks) if (canPlay(t)) t];
      if (usable.isEmpty) return;
      if (shuffle) usable.shuffle();
      final start = shuffle ? 0 : usable.indexWhere((t) => t.id == tracks[index.clamp(0, tracks.length - 1)].id);
      return jam!.replace(usable, start < 0 ? 0 : start);
    }
    final usable = <Track>[];
    var start = 0;
    for (var i = 0; i < tracks.length; i++) {
      final t = tracks[i];
      if (canPlay(t)) {
        if (i == index) start = usable.length;
        usable.add(t);
      } else if (i == index) {
        _messages.add('"${t.title}" is still being downloaded to your server. ${t.statusText}');
        start = usable.length; // continue with the next song that can play
      }
    }
    if (usable.isEmpty) return;
    if (start >= usable.length) start = 0;
    _previewing = false; // choosing music ends any preview for good
    _beforePreview = null;
    if (shuffle && index == 0) start = Random().nextInt(usable.length);
    _reportCurrent();
    _queue = usable;
    _queueChanged();
    final first = usable[start];
    final resume = first.isEpisode && !first.completed && first.progress > const Duration(seconds: 15) &&
            (first.duration == Duration.zero || first.progress < first.duration - const Duration(seconds: 30))
        ? first.progress
        : null;
    _lastSaved = resume ?? Duration.zero;
    await player.setAudioSources([for (final t in usable) _source(t)], initialIndex: start, initialPosition: resume);
    await player.setSpeed(first.isEpisode ? _podcastSpeed : 1.0);
    await player.setShuffleModeEnabled(shuffle);
    if (shuffle) await player.shuffle(); // keeps the starting song first
    unawaited(player.play());
  }

  /// Play a short preview (a clip, or part of a song on the server from [start]) without losing
  /// the queue: [endPreview] puts back whatever was playing, where it was.
  Future<void> preview(Track t, {Duration? start}) async {
    if (jam != null) {
      _messages.add('Previews are paused during a Jam.');
      return;
    }
    if (!_previewing) {
      _reportCurrent();
      final i = player.currentIndex;
      _beforePreview = _queue.isEmpty || i == null
          ? null
          : (queue: _queue, index: i, position: player.position, playing: player.playing, loop: player.loopMode);
    }
    _previewing = true;
    _lastIndex = null;
    _queue = [t];
    _queueChanged();
    await player.setLoopMode(LoopMode.off);
    await player.setShuffleModeEnabled(false);
    await player.setAudioSources([_source(t)], initialIndex: 0, initialPosition: start);
    await player.setSpeed(1.0);
    unawaited(player.play());
  }

  Future<void> endPreview() async {
    if (!_previewing) return;
    final before = _beforePreview;
    _beforePreview = null;
    _lastIndex = null; // the preview is never reported as a listen
    await player.pause(); // "playing" outlives the source: only resume if it was playing before
    if (before == null) {
      await handler.stop();
      _queue = const [];
      _queueChanged();
    } else {
      _queue = before.queue;
      _queueChanged();
      await player.setAudioSources([for (final t in before.queue) _source(t)], initialIndex: before.index, initialPosition: before.position);
      await player.setLoopMode(before.loop);
      await player.setSpeed(before.queue[before.index].isEpisode ? _podcastSpeed : 1.0);
      if (before.playing) unawaited(player.play());
    }
    _previewing = false;
  }

  Future<void> playNext(Track t) => _insert(t, (player.currentIndex ?? -1) + 1);
  Future<void> addToQueue(Track t) => _insert(t, _queue.length);

  Future<void> _insert(Track t, int at) async {
    if (!canPlay(t)) {
      _messages.add('"${t.title}" is still being downloaded to your server.');
      return;
    }
    if (jam != null) return jam!.add([t], next: at < _queue.length); // into the Jam's queue, for everyone
    if (_queue.isEmpty) return play([t]);
    final i = at.clamp(0, _queue.length);
    _queue = [..._queue]..insert(i, t);
    _queueChanged();
    await player.insertAudioSource(i, _source(t));
  }

  Future<void> removeAt(int i) async {
    if (jam != null) {
      _messages.add('The Jam\'s queue can only grow: skip songs you don\'t want.');
      return;
    }
    if (i < 0 || i >= _queue.length || i == player.currentIndex) return;
    _queue = [..._queue]..removeAt(i);
    _queueChanged();
    await player.removeAudioSourceAt(i);
  }

  Future<void> togglePlay() {
    if (jam != null) return jam!.setPlaying(!player.playing);
    return player.playing ? player.pause() : player.play();
  }

  /// Seek in the current song (in a Jam, for everyone).
  Future<void> seek(Duration position) => jam != null ? jam!.seekTo(position) : player.seek(position);

  /// Play queue item [index] from the start.
  Future<void> jumpTo(int index) => jam != null ? jam!.jumpTo(index) : player.seek(Duration.zero, index: index);

  /// Podcasts: jump back or ahead within the episode.
  Future<void> skipBy(Duration d) async {
    final total = player.duration;
    var to = player.position + d;
    if (to < Duration.zero) to = Duration.zero;
    if (total != null && to > total) to = total;
    await seek(to);
  }

  double get podcastSpeed => _podcastSpeed;

  /// Playback speed for podcasts (songs always play at normal speed). Remembered between episodes.
  Future<void> setPodcastSpeed(double speed) async {
    if (jam != null) return; // a Jam plays at 1x so everyone stays together
    _podcastSpeed = speed;
    if (current?.isEpisode == true) await player.setSpeed(speed);
    try {
      (await SharedPreferences.getInstance()).setDouble('podcast_speed', speed);
    } catch (_) {
      // not remembered; it still applies now
    }
  }

  Future<void> next() async {
    if (jam != null) return jam!.skip(1);
    if (player.hasNext) await player.seekToNext();
  }

  /// Like most players: restart the song if it's past 3 seconds, otherwise go back one.
  Future<void> previous() async {
    if (jam != null) {
      return player.position > const Duration(seconds: 3) || _jamIndex == 0 ? jam!.seekTo(Duration.zero) : jam!.skip(-1);
    }
    if (player.position > const Duration(seconds: 3) || !player.hasPrevious) {
      await player.seek(Duration.zero);
    } else {
      await player.seekToPrevious();
    }
  }

  Future<void> toggleShuffle() async {
    if (jam != null) return;
    final on = !player.shuffleModeEnabled;
    if (on) await player.shuffle();
    await player.setShuffleModeEnabled(on);
  }

  Future<void> cycleRepeat() async => jam != null ? null : player.setLoopMode(switch (player.loopMode) {
        LoopMode.off => LoopMode.all,
        LoopMode.all => LoopMode.one,
        LoopMode.one => LoopMode.off,
      });

  /// Make the player match a Jam (called by the Jam itself, so nothing here is sent back to it).
  Future<void> applyJam(List<Track> queue, int index, Duration position, bool playing) async {
    if (index < 0 || index >= queue.length) return;
    final item = queue[index];
    final loaded = _jamQueue != null && _queue.length == 1 && _queue.first.id == item.id && player.processingState != ProcessingState.idle;
    _jamQueue = queue;
    _jamIndex = index;
    if (!loaded) {
      _previewing = false;
      _beforePreview = null;
      _reportCurrent();
      _queue = [item];
      _queueChanged();
      await player.setLoopMode(LoopMode.off);
      await player.setShuffleModeEnabled(false);
      await player.setAudioSources([_source(item)], initialIndex: 0, initialPosition: position);
      await player.setSpeed(1.0); // everyone at the same speed, podcasts too
    } else {
      _queueChanges.add(queue);
      _currentChanges.add(current);
      if ((player.position - position).abs() > const Duration(milliseconds: 350)) await player.seek(position);
    }
    if (playing && !player.playing) unawaited(player.play());
    if (!playing && player.playing) await player.pause();
  }

  /// The Jam is over for this phone: keep playing what's on, as an ordinary queue.
  void leftJam() {
    jam = null;
    _jamQueue = null;
    _queueChanges.add(queue);
    _currentChanges.add(current);
    player.setSpeed(current?.isEpisode == true ? _podcastSpeed : 1.0); // the Jam may have been nudging it
  }

  // -- picking up where you left off ---------------------------------------------------------

  static const _sessionKey = 'last_session';
  static const _sessionMax = 300; // songs kept around the current one (Liked Songs can run to thousands)
  DateTime _sessionSaved = DateTime.fromMillisecondsSinceEpoch(0);
  bool _restoring = false;
  String? _resumedId; // the song brought back paused: only what's heard after reopening counts as a listen
  Duration _resumedFrom = Duration.zero;

  void _saveSessionSoon() {
    if (DateTime.now().difference(_sessionSaved) > const Duration(seconds: 5)) _saveSession();
  }

  /// Remember what's playing on this phone: the queue, the song and where in it. Previews aren't
  /// saved (what was playing before them is what comes back).
  Future<void> _saveSession() async {
    if (_previewing || _restoring) return;
    _sessionSaved = DateTime.now();
    final session = ref.read(sessionProvider).value;
    final q = queue;
    final i = currentIndex;
    try {
      final prefs = await SharedPreferences.getInstance();
      if (session == null || q.isEmpty || i == null || i >= q.length) {
        await prefs.remove(_sessionKey);
        return;
      }
      final from = max(0, min(i - 50, q.length - _sessionMax));
      final kept = q.sublist(from, min(q.length, from + _sessionMax));
      await prefs.setString(_sessionKey, jsonEncode({
        'server': session.server,
        'user': session.userId,
        'queue': [for (final t in kept) t.json],
        'index': i - from,
        'position_ms': player.position.inMilliseconds,
        'shuffle': _jamQueue == null && player.shuffleModeEnabled,
        'loop': _jamQueue == null ? player.loopMode.index : 0,
        'saved': DateTime.now().millisecondsSinceEpoch,
      }));
    } catch (_) {
      // storage unavailable: nothing to bring back next time
    }
  }

  /// When the app opens, put back the last queue, paused at the same song and spot. Nothing is
  /// reported as a listen. Does nothing if something is already playing or it was another account.
  Future<void> restoreLastSession() async {
    if (_queue.isNotEmpty || jam != null) return;
    final session = ref.read(sessionProvider).value;
    if (session == null) return;
    try {
      final raw = (await SharedPreferences.getInstance()).getString(_sessionKey);
      if (raw == null) return;
      final j = jsonDecode(raw) as Map<String, dynamic>;
      if (j['server'] != session.server || j['user'] != session.userId) return;
      final tracks = [for (final t in j['queue'] as List) Track.fromJson(Map<String, dynamic>.from(t as Map))];
      final index = (j['index'] as num).toInt();
      if (tracks.isEmpty || index < 0 || index >= tracks.length || _queue.isNotEmpty || jam != null) return;
      var position = Duration(milliseconds: (j['position_ms'] as num?)?.toInt() ?? 0);
      final t = tracks[index];
      if (t.duration > Duration.zero && position >= t.duration - const Duration(seconds: 2)) position = Duration.zero;
      _restoring = true;
      _resumedId = t.id;
      _resumedFrom = position;
      _queue = tracks;
      _queueChanged();
      await player.setLoopMode(LoopMode.values[((j['loop'] as num?)?.toInt() ?? 0).clamp(0, LoopMode.values.length - 1)]);
      await player.setAudioSources([for (final x in tracks) _source(x)], initialIndex: index, initialPosition: position);
      await player.setSpeed(t.isEpisode ? _podcastSpeed : 1.0);
      if (j['shuffle'] == true) {
        await player.setShuffleModeEnabled(true);
        await player.shuffle(); // keeps the current song where it is
      }
      _currentChanges.add(current);
    } catch (_) {
      // the server is unreachable or the saved queue is unreadable: start fresh
      if (_restoring && !player.playing) {
        _queue = const [];
        _queueChanged();
      }
    } finally {
      _restoring = false;
    }
  }

  void _onIndex(int? i) {
    if (i == _lastIndex) return;
    final prev = _lastIndex;
    if (prev != null && prev < _queue.length) _report(_queue[prev], _lastPosition);
    _lastIndex = i;
    _lastPosition = Duration.zero;
    _lastSaved = Duration.zero;
    final now = current;
    _offline.protectedId = now?.id;
    unawaited(_offline.trimCache()); // the song that just finished may have filled the cache
    unawaited(_saveSession());
    _fetchAhead();
  }

  /// How many songs the phone keeps ahead of the one playing.
  static const ahead = 3;

  /// The next [ahead] songs (in the order they'll play, shuffled or not) onto the phone.
  void _fetchAhead() {
    final api = ref.read(apiProvider), i = player.currentIndex;
    if (api == null || i == null || _previewing || _queue.isEmpty) return;
    final order = player.shuffleModeEnabled ? [...player.shuffleIndices] : [for (var k = 0; k < _queue.length; k++) k];
    final at = order.indexOf(i);
    if (at < 0) return;
    final next = [for (final k in order.skip(at + 1).take(ahead)) if (k < _queue.length) _queue[k]];
    unawaited(_offline.fetchAhead(api, next));
  }

  void _reportCurrent() {
    final i = _lastIndex;
    if (i != null && i < _queue.length) _report(_queue[i], _lastPosition);
    _lastIndex = null;
  }

  void _saveEpisode(Track t, Duration position) {
    _lastSaved = position;
    final total = player.duration ?? (t.duration == Duration.zero ? null : t.duration);
    final done = total != null && position >= total - const Duration(seconds: 30);
    ref.read(apiProvider)?.saveEpisodeProgress(t.id, position, total, done).then((_) {
      ref.invalidate(podcastsProvider);
      ref.invalidate(podcastProvider(t.podcastId ?? ''));
      if (done) _offline.syncEpisodesSoon(); // "until played" downloads go once heard
    }).catchError((_) {});
  }

  void _report(Track t, Duration played) {
    if (_previewing || t.isPreview) return;
    if (t.isEpisode) {
      if (played > const Duration(seconds: 1)) _saveEpisode(t, played);
      return;
    }
    if (t.id == _resumedId) {
      played -= _resumedFrom; // it was brought back part-way through
      _resumedId = null;
    }
    if (played < const Duration(seconds: 5)) return;
    unawaited(_offline.recordListen(t.id, played, t.duration));
    final completed = t.duration > Duration.zero && played >= t.duration - const Duration(seconds: 3);
    ref.read(apiProvider)?.reportPlay(t.id, played, completed).catchError((_) {}); // offline: the device score still counts
  }

  Future<void> stop() async {
    _reportCurrent();
    await handler.stop();
    _queue = const [];
    _queueChanged();
    unawaited(_saveSession()); // nothing playing: nothing to bring back
  }

  void dispose() {
    for (final s in _subs) {
      s.cancel();
    }
    likes.dispose(); // the player belongs to the media session and lives as long as the app
    _messages.close();
    _queueChanges.close();
    _currentChanges.close();
  }
}

final playerProvider = Provider<PlayerController>((ref) {
  final c = PlayerController(ref, ref.watch(audioHandlerProvider));
  ref.onDispose(c.dispose);
  return c;
});
