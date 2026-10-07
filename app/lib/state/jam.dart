import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:just_audio/just_audio.dart';

import '../api/client.dart';
import '../api/models.dart';
import 'player.dart';
import 'session.dart';

class JamState {
  const JamState({this.jam, this.invites = const []});
  final Jam? jam;
  final List<JamInvite> invites;
}

/// Listening together. While in a Jam this phone follows the server's shared timeline:
/// it waits for changes (a long poll), starts songs at the agreed moment using its estimate
/// of the server's clock, nudges itself back into step if it drifts, and tells the Jam when
/// the song ends. Your own play/pause/seek/skip go to the Jam (see [JamLink]) so everyone hears them.
class JamController extends Notifier<JamState> implements JamLink {
  Timer? _invitePoll;
  Timer? _drift;
  Timer? _start;
  StreamSubscription<PlayerState>? _ended;
  int _offsetMs = 0; // server clock minus this phone's clock
  int _bestRtt = 1 << 30;
  String? _following; // id of the Jam the long poll is following
  final _seenInvites = <String>{};

  @override
  JamState build() {
    ref.watch(apiProvider); // signing out (or into another server) starts afresh
    _invitePoll = Timer.periodic(const Duration(seconds: 20), (_) => refresh());
    ref.onDispose(() {
      _invitePoll?.cancel();
      _stopFollowing();
    });
    Future.microtask(refresh);
    return const JamState();
  }

  PlayerController get _player => ref.read(playerProvider);
  SongarrApi? get _api => ref.read(apiProvider);
  int get _serverNow => DateTime.now().millisecondsSinceEpoch + _offsetMs;

  /// Learn the server's clock from a quick request (the fastest round trips are the most accurate).
  void _clock(int serverMs, int sentMs, int gotMs) {
    final rtt = gotMs - sentMs;
    if (serverMs <= 0 || rtt > 3000) return;
    if (rtt <= _bestRtt * 1.5 || rtt < 150) {
      _bestRtt = rtt < _bestRtt ? rtt : _bestRtt;
      _offsetMs = serverMs - (sentMs + gotMs) ~/ 2;
    }
  }

  /// Check for invites, and pick up a Jam this profile is already in (e.g. after the app restarted).
  Future<void> refresh() async {
    final api = _api;
    if (api == null) return;
    try {
      final sent = DateTime.now().millisecondsSinceEpoch;
      final (jam, invites, serverTime) = await api.currentJam();
      _clock(serverTime, sent, DateTime.now().millisecondsSinceEpoch);
      state = JamState(jam: state.jam ?? jam, invites: invites);
      if (jam != null && _following != jam.id) _follow(jam);
    } on ApiException {
      // offline or signed out: try again later
    }
  }

  /// Show invite [id] again (its notification was tapped).
  void announceAgain(String id) => _seenInvites.remove(id);

  /// Invites not yet shown to the user (each is announced once).
  List<JamInvite> takeNewInvites() {
    final fresh = [for (final i in state.invites) if (_seenInvites.add(i.id)) i];
    return fresh;
  }

  // -- starting, joining, leaving ---------------------------------------------------------------

  /// Start a Jam from what's playing (or from the list [tracks], at [index]) and invite [people].
  /// The Jam plays on through that list, and round again, until someone picks from another one.
  Future<void> start(List<int> people, {List<Track>? tracks, int index = 0}) async {
    final api = _api;
    if (api == null) return;
    final p = _player;
    final fromQueue = tracks == null;
    var list = tracks ?? p.queue;
    var at = fromQueue ? (p.currentIndex ?? 0) : index;
    final order = p.player.effectiveIndices;
    if (fromQueue && p.player.shuffleModeEnabled && order.length == list.length) {
      // shuffling: the Jam plays in the order you were about to hear
      list = [for (final i in order) list[i]];
      at = order.indexOf(at);
    }
    final (items, start) = _forJam(list, at);
    if (items.isEmpty) throw ApiException('Play something first, then start a Jam.');
    final sameSong = fromQueue && p.current?.id == items[start].id;
    final jam = await api.startJam(items, start, sameSong ? p.player.position : Duration.zero,
        fromQueue ? (p.player.playing || p.current == null) : true, people);
    _follow(jam);
  }

  /// What the Jam can play from [list] (everyone streams from the server, so only songs it has),
  /// and where [index] ends up; a very long list is cut to a window around it.
  static (List<Track>, int) _forJam(List<Track> list, int index) {
    if (list.isEmpty) return (const [], 0);
    final want = list[index.clamp(0, list.length - 1)].id;
    final usable = [for (final t in list) if (t.isEpisode || t.playable) t];
    var at = usable.indexWhere((t) => t.id == want);
    if (at < 0) at = 0;
    const max = 500;
    if (usable.length <= max) return (usable, at);
    final from = (at - 50).clamp(0, usable.length - max);
    return (usable.sublist(from, from + max), at - from);
  }

  Future<void> invite(List<int> people) async {
    final jam = state.jam;
    if (jam == null) return;
    _apply(await _api!.inviteToJam(jam.id, people));
  }

  Future<void> join(String id) async {
    final api = _api;
    if (api == null) return;
    final sent = DateTime.now().millisecondsSinceEpoch;
    final jam = await api.joinJam(id);
    _clock(jam.serverTime, sent, DateTime.now().millisecondsSinceEpoch);
    state = JamState(jam: jam, invites: [for (final i in state.invites) if (i.id != id) i]);
    _follow(jam);
  }

  Future<void> decline(String id) async {
    state = JamState(jam: state.jam, invites: [for (final i in state.invites) if (i.id != id) i]);
    await _api?.declineJam(id).catchError((_) {});
  }

  Future<void> leave() async {
    final jam = state.jam;
    if (jam == null) return;
    _stopFollowing();
    state = JamState(invites: state.invites);
    await _api?.leaveJam(jam.id).catchError((_) {});
  }

  void _follow(Jam jam) {
    _stopFollowing();
    _following = jam.id;
    _player.jam = this;
    _apply(jam);
    _drift = Timer.periodic(_driftEvery, (_) => _correctDrift());
    _ended = _player.player.playerStateStream.listen((s) {
      if (s.processingState == ProcessingState.completed) _songEnded();
    });
    unawaited(_poll(jam.id));
  }

  /// The song finished here: move the Jam on (only once, however many phones say so). Only
  /// when the shared timeline agrees it's over, so a stale "finished" from before joining
  /// can't skip anything.
  void _songEnded() {
    final j = state.jam;
    final length = _player.player.duration ?? j?.current?.duration;
    if (j == null || !j.playing || length == null || _player.current?.id != j.current?.id) return;
    if (j.positionAt(_serverNow) >= length - const Duration(seconds: 3)) {
      _control('next', {'expected_index': j.index}, true);
    }
  }

  void _stopFollowing() {
    _following = null;
    _drift?.cancel();
    _start?.cancel();
    _ended?.cancel();
    if (_player.jam == this) _player.leftJam();
  }

  Future<void> _poll(String id) async {
    var failures = 0;
    while (_following == id) {
      final jam = state.jam;
      final api = _api;
      if (jam == null || api == null) return;
      try {
        final next = await api.waitForJam(id, jam.version);
        failures = 0;
        if (_following != id) return;
        if (next.ended) return _ended404();
        if (next.version != jam.version) _apply(next);
      } on ApiException catch (e) {
        if (e.status == 404 || e.status == 403) return _ended404();
        failures++;
        await Future<void>.delayed(Duration(seconds: failures < 5 ? 2 : 10)); // offline for a moment: keep trying
      }
    }
  }

  void _ended404() {
    _stopFollowing();
    state = JamState(invites: state.invites);
    _player.say('The Jam has ended. You\'re listening on your own now.');
  }

  // -- following the shared timeline -------------------------------------------------------------

  void _apply(Jam jam) {
    final old = state.jam;
    if (old != null && jam.id == old.id && jam.version < old.version) return; // late news
    state = JamState(jam: jam, invites: state.invites);
    _start?.cancel();
    final now = _serverNow;
    final queue = jam.queue;
    if (queue.isEmpty) return;
    if (jam.playing && jam.ref > now) {
      // everyone starts together: get ready, then go at the agreed moment
      _player.applyJam(queue, jam.index, jam.position, false);
      _start = Timer(Duration(milliseconds: jam.ref - now), () {
        if (state.jam?.version == jam.version) _player.applyJam(queue, jam.index, jam.positionAt(_serverNow), true);
      });
    } else {
      _player.applyJam(queue, jam.index, jam.positionAt(now), jam.playing);
    }
  }

  /// Stay on the shared timeline. Starting playback takes the phone a moment, so it tends to
  /// sit a little behind: small gaps are closed by playing up to 5% faster or slower until the
  /// next check (not noticeable, and no skip in the sound); big ones by jumping.
  void _correctDrift() {
    final jam = state.jam;
    final p = _player.player;
    if (p.processingState == ProcessingState.completed) return _songEnded(); // in case it finished a little early
    if (jam == null || !jam.playing || !p.playing || p.processingState != ProcessingState.ready) return _nudge(1.0);
    final now = _serverNow;
    if (now < jam.ref) return;
    final off = (p.position - jam.positionAt(now)).inMilliseconds; // + ahead, - behind
    if (off.abs() > 400) {
      _nudge(1.0);
      p.seek(jam.positionAt(_serverNow));
    } else if (off.abs() > 30) {
      _nudge((1 - off / _driftEvery.inMilliseconds).clamp(0.95, 1.05)); // closes the gap by the next check
    } else {
      _nudge(1.0);
    }
  }

  static const _driftEvery = Duration(seconds: 2);

  void _nudge(double speed) {
    final p = _player.player;
    if ((p.speed - speed).abs() > 0.001) p.setSpeed(speed);
  }

  /// Change the Jam for everyone. [quiet] for the phone's own automatic steps (no message if offline).
  Future<void> _control(String action, [Map<String, dynamic> extra = const {}, bool quiet = false]) async {
    final jam = state.jam;
    final api = _api;
    if (jam == null || api == null) return;
    try {
      final sent = DateTime.now().millisecondsSinceEpoch;
      final next = await api.controlJam(jam.id, action, extra);
      _clock(next.serverTime, sent, DateTime.now().millisecondsSinceEpoch);
      _apply(next);
    } on ApiException catch (e) {
      if (e.status == 404 || e.status == 403) {
        _ended404();
      } else if (!quiet) {
        _player.say('Can\'t reach the Jam right now. (To listen on your own, leave the Jam.)');
      }
    }
  }

  // -- JamLink: this person's own actions, for everyone -------------------------------------------

  @override
  Future<void> setPlaying(bool playing) async {
    if (!playing) await _player.player.pause(); // feels instant here; everyone else follows
    await _control(playing ? 'play' : 'pause', {'position_ms': _player.player.position.inMilliseconds});
  }

  @override
  Future<void> seekTo(Duration position) => _control('seek', {'position_ms': position.inMilliseconds});

  @override
  Future<void> skip(int delta) => _control(delta > 0 ? 'next' : 'prev', {'expected_index': state.jam?.index});

  @override
  Future<void> jumpTo(int index) => _control('jump', {'index': index, 'expected_index': state.jam?.index});

  @override
  Future<void> replace(List<Track> tracks, int index) {
    final (items, at) = _forJam(tracks, index);
    if (items.isEmpty) {
      _player.say('That isn\'t on your server yet, so the Jam can\'t play it.');
      return Future.value();
    }
    return _control('replace', {'items': SongarrApi.jamItems(items), 'index': at});
  }

  @override
  Future<void> add(List<Track> tracks, {bool next = false}) =>
      _control('add', {'items': SongarrApi.jamItems(tracks), 'next': next});
}

final jamProvider = NotifierProvider<JamController, JamState>(JamController.new);
