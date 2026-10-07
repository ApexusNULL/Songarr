import 'dart:async';
import 'dart:ui' show ImageFilter;

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:just_audio/just_audio.dart';

import '../api/models.dart';
import '../state/data.dart';
import '../state/player.dart';
import 'fx/colors.dart';
import 'fx/motion.dart';
import 'fx/nebula.dart';
import 'fx/sparkle.dart';
import 'theme.dart';
import 'widgets.dart';

/// Full-screen lyrics for whatever is playing; synced lyrics follow the song line by line.
class LyricsScreen extends ConsumerWidget {
  const LyricsScreen({super.key});

  static Route<void> route() => PageRouteBuilder<void>(
        transitionDuration: const Duration(milliseconds: 420),
        reverseTransitionDuration: const Duration(milliseconds: 320),
        pageBuilder: (_, _, _) => const LyricsScreen(),
        transitionsBuilder: (context, a, _, child) {
          final curve = CurvedAnimation(parent: a, curve: Curves.easeOutCubic, reverseCurve: Curves.easeInCubic);
          return FadeTransition(
            opacity: curve,
            child: SlideTransition(position: Tween(begin: const Offset(0, 0.08), end: Offset.zero).animate(curve), child: child),
          );
        },
      );

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final c = ref.watch(playerProvider);
    return Scaffold(
      backgroundColor: background,
      body: StreamBuilder<Track?>(
        stream: c.currentStream,
        initialData: c.current,
        builder: (context, snap) {
          final t = snap.data;
          final tint = t?.coverUrl == null ? null : ref.watch(artColorProvider(t!.coverUrl!)).value;
          return Stack(children: [
            Positioned.fill(child: NebulaBackground(tint: tint, intensity: 1.2)),
            if (t?.coverUrl != null)
              Positioned.fill(
                child: Opacity(
                  opacity: 0.18,
                  child: ImageFiltered(
                    imageFilter: ImageFilter.blur(sigmaX: 60, sigmaY: 60),
                    child: CachedNetworkImage(imageUrl: t!.coverUrl!, fit: BoxFit.cover, memCacheWidth: 200, errorWidget: (_, _, _) => const SizedBox()),
                  ),
                ),
              ),
            SafeArea(
              child: Column(children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
                  child: Row(children: [
                    GlassIconButton(icon: Icons.keyboard_arrow_down_rounded, tooltip: 'Close', onPressed: () => Navigator.pop(context)),
                    const SizedBox(width: 12),
                    if (t != null) Cover(t.thumbUrl, size: 42),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        Text(t?.title ?? '', maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w800)),
                        Text(t?.artistLine ?? '', maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 12.5)),
                      ]),
                    ),
                  ]),
                ),
                Expanded(
                  child: t == null || t.isEpisode || t.isPreview
                      ? const _Message('Lyrics show here while a song plays.')
                      : ref.watch(lyricsProvider(t.id)).when(
                            loading: () => const Center(child: CircularProgressIndicator()),
                            error: (e, _) => _Message('Couldn\'t load lyrics: $e'),
                            data: (l) => l == null
                                ? const _Message('No lyrics found for this song.')
                                : l.instrumental && !l.isSynced && l.plain.isEmpty
                                    ? const _Message('♪  Instrumental  ♪')
                                    : l.isSynced
                                        ? _SyncedLyrics(key: ValueKey(t.id), lines: l.synced, controller: c)
                                        : _PlainLyrics(text: l.plain),
                          ),
                ),
                if (t != null) _Transport(controller: c, track: t),
              ]),
            ),
          ]);
        },
      ),
    );
  }
}

class _Message extends StatelessWidget {
  const _Message(this.text);
  final String text;
  @override
  Widget build(BuildContext context) =>
      Center(child: Padding(padding: const EdgeInsets.all(32), child: Text(text, textAlign: TextAlign.center, style: const TextStyle(color: muted, fontSize: 16))));
}

class _PlainLyrics extends StatelessWidget {
  const _PlainLyrics({required this.text});
  final String text;

  @override
  Widget build(BuildContext context) => ListView(padding: const EdgeInsets.fromLTRB(24, 24, 24, 40), children: [
        const Text('These lyrics aren\'t timed to the song.', style: TextStyle(color: muted, fontSize: 12.5)),
        const SizedBox(height: 16),
        Text(text, style: const TextStyle(fontSize: 21, fontWeight: FontWeight.w700, height: 1.5)),
        const SizedBox(height: 24),
        const Text('Lyrics from LRCLIB', style: TextStyle(color: muted, fontSize: 11.5)),
      ]);
}

class _SyncedLyrics extends StatefulWidget {
  const _SyncedLyrics({super.key, required this.lines, required this.controller});
  final List<({Duration at, String text})> lines;
  final PlayerController controller;

  @override
  State<_SyncedLyrics> createState() => _SyncedLyricsState();
}

class _SyncedLyricsState extends State<_SyncedLyrics> {
  late final _keys = List.generate(widget.lines.length, (_) => GlobalKey());
  StreamSubscription<Duration>? _positions;
  int _current = -1;
  DateTime _userScrolled = DateTime.fromMillisecondsSinceEpoch(0);

  @override
  void initState() {
    super.initState();
    _positions = widget.controller.player.positionStream.listen(_onPosition);
    WidgetsBinding.instance.addPostFrameCallback((_) => _onPosition(widget.controller.player.position, jump: true));
  }

  @override
  void dispose() {
    _positions?.cancel();
    super.dispose();
  }

  int _lineAt(Duration p) {
    // the last line that has started (lines are in time order)
    var lo = 0, hi = widget.lines.length - 1, found = -1;
    while (lo <= hi) {
      final mid = (lo + hi) >> 1;
      if (widget.lines[mid].at <= p + const Duration(milliseconds: 150)) {
        found = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return found;
  }

  void _onPosition(Duration p, {bool jump = false}) {
    final i = _lineAt(p);
    if (i == _current || !mounted) return;
    setState(() => _current = i);
    if (DateTime.now().difference(_userScrolled) < const Duration(seconds: 4)) return; // let them read ahead
    final ctx = i >= 0 ? _keys[i].currentContext : null;
    if (ctx != null) {
      Scrollable.ensureVisible(ctx,
          alignment: 0.4, duration: jump ? Duration.zero : const Duration(milliseconds: 450), curve: Curves.easeOutCubic);
    }
  }

  @override
  Widget build(BuildContext context) {
    return NotificationListener<UserScrollNotification>(
      onNotification: (_) {
        _userScrolled = DateTime.now();
        return false;
      },
      child: ShaderMask(
        // fade the lines out toward the top and bottom edges
        blendMode: BlendMode.dstIn,
        shaderCallback: (r) => const LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [Colors.transparent, Colors.white, Colors.white, Colors.transparent],
          stops: [0, 0.12, 0.85, 1],
        ).createShader(r),
        child: SingleChildScrollView(
          padding: EdgeInsets.fromLTRB(24, MediaQuery.sizeOf(context).height * 0.25, 24, MediaQuery.sizeOf(context).height * 0.4),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            for (var i = 0; i < widget.lines.length; i++)
              GestureDetector(
                key: _keys[i],
                behavior: HitTestBehavior.opaque,
                onTap: () {
                  _userScrolled = DateTime.fromMillisecondsSinceEpoch(0);
                  widget.controller.seek(widget.lines[i].at);
                },
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 9),
                  child: _Line(text: widget.lines[i].text, state: i == _current ? 0 : (i < _current ? -1 : 1)),
                ),
              ),
            const SizedBox(height: 24),
            const Text('Lyrics from LRCLIB', style: TextStyle(color: muted, fontSize: 11.5)),
          ]),
        ),
      ),
    );
  }
}

/// A line: the one being sung glows in the aurora gradient; sung lines dim; upcoming ones wait.
class _Line extends StatelessWidget {
  const _Line({required this.text, required this.state});
  final String text;
  final int state; // -1 sung, 0 now, 1 upcoming

  @override
  Widget build(BuildContext context) {
    final words = text.isEmpty ? '♪' : text;
    final style = TextStyle(fontSize: state == 0 ? 27 : 23, fontWeight: FontWeight.w800, height: 1.3, letterSpacing: -0.3);
    return AnimatedScale(
      scale: state == 0 ? 1.0 : 0.97,
      alignment: Alignment.centerLeft,
      duration: const Duration(milliseconds: 350),
      curve: Curves.easeOutCubic,
      child: AnimatedOpacity(
        opacity: state == 0 ? 1 : (state < 0 ? 0.35 : 0.6),
        duration: const Duration(milliseconds: 350),
        child: state == 0
            ? Container(
                decoration: BoxDecoration(boxShadow: [BoxShadow(color: accent2.withValues(alpha: 0.18), blurRadius: 30)]),
                child: GradientMask(gradient: const LinearGradient(colors: [Colors.white, glowColor, Color(0xFFF9A8D4)]), child: Text(words, style: style)),
              )
            : Text(words, style: style),
      ),
    );
  }
}

/// Play/pause and progress, so the song can be controlled without leaving the lyrics.
class _Transport extends StatelessWidget {
  const _Transport({required this.controller, required this.track});
  final PlayerController controller;
  final Track track;

  @override
  Widget build(BuildContext context) {
    final p = controller.player;
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 4, 20, 14),
      child: StreamBuilder<PlayerState>(
        stream: p.playerStateStream,
        initialData: p.playerState,
        builder: (context, s) {
          final playing = s.data?.playing == true;
          return Row(children: [
            Expanded(
              child: StreamBuilder<Duration>(
                stream: p.positionStream,
                builder: (context, ps) => SparkleBar(
                  position: ps.data ?? Duration.zero,
                  duration: p.duration ?? track.duration,
                  playing: playing,
                  onSeek: controller.seek,
                  height: 30,
                  trackHeight: 3,
                  sparklesPerSecond: 8,
                ),
              ),
            ),
            const SizedBox(width: 14),
            AuroraPlayButton(playing: playing, onPressed: controller.togglePlay, size: 52),
          ]);
        },
      ),
    );
  }
}
