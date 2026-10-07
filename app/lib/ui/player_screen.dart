import 'dart:math';
import 'dart:ui' show ImageFilter, lerpDouble;

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:just_audio/just_audio.dart';

import '../api/models.dart';
import '../state/jam.dart';
import '../state/offline.dart';
import '../state/player.dart';
import 'fx/colors.dart';
import 'fx/motion.dart';
import 'fx/nebula.dart';
import 'fx/sparkle.dart';
import 'jam_sheet.dart';
import 'lyrics_screen.dart';
import 'theme.dart';
import 'tracklist_screen.dart';
import 'widgets.dart';

/// Full-screen "now playing": the room takes on the colour of the cover art.
/// The song's artist page (with several artists, pick one first). It opens over Now Playing;
/// back returns to it.
Future<void> openArtistOf(BuildContext context, Track t) async {
  var artist = t.artists.first;
  if (t.artists.length > 1) {
    final picked = await showModalBottomSheet<String>(
      context: context,
      useRootNavigator: true,
      builder: (sheet) => SafeArea(
        child: ListView(shrinkWrap: true, children: [
          const Padding(
            padding: EdgeInsets.fromLTRB(20, 16, 20, 4),
            child: Text('Artists', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 18)),
          ),
          for (final a in t.artists)
            ListTile(leading: const Icon(Icons.person_rounded), title: Text(a), onTap: () => Navigator.pop(sheet, a)),
        ]),
      ),
    );
    if (picked == null) return;
    artist = picked;
  }
  if (context.mounted) push(context, ArtistScreen(artist));
}

class PlayerScreen extends ConsumerWidget {
  const PlayerScreen({super.key});

  static Route<void> route() => PageRouteBuilder<void>(
        transitionDuration: const Duration(milliseconds: 480),
        reverseTransitionDuration: const Duration(milliseconds: 380),
        pageBuilder: (_, _, _) => const PlayerScreen(),
        transitionsBuilder: (context, a, _, child) {
          final curve = CurvedAnimation(parent: a, curve: Curves.easeOutCubic, reverseCurve: Curves.easeInCubic);
          return SlideTransition(
            position: Tween(begin: const Offset(0, 0.25), end: Offset.zero).animate(curve),
            child: FadeTransition(opacity: curve, child: child),
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
          if (t == null) {
            return Stack(children: [
              const Positioned.fill(child: NebulaBackground()),
              SafeArea(
                child: Column(children: [
                  Align(alignment: Alignment.centerLeft, child: _TopBar(track: null, onQueue: null)),
                  const Expanded(child: Center(child: Text('Nothing playing', style: TextStyle(color: muted)))),
                ]),
              ),
            ]);
          }
          final tint = t.coverUrl == null ? null : ref.watch(artColorProvider(t.coverUrl!)).value;
          return Stack(children: [
            Positioned.fill(child: NebulaBackground(tint: tint, intensity: 1.15)),
            if (t.coverUrl != null)
              Positioned.fill(
                child: Opacity(
                  opacity: 0.22,
                  child: ImageFiltered(
                    imageFilter: ImageFilter.blur(sigmaX: 50, sigmaY: 50),
                    child: CachedNetworkImage(imageUrl: t.coverUrl!, fit: BoxFit.cover, memCacheWidth: 200, errorWidget: (_, _, _) => const SizedBox()),
                  ),
                ),
              ),
            const Positioned.fill(
              child: DecoratedBox(
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    begin: Alignment.topCenter,
                    end: Alignment.bottomCenter,
                    colors: [Color(0x3307060F), Color(0x0007060F), Color(0xCC07060F)],
                    stops: [0, 0.4, 1],
                  ),
                ),
              ),
            ),
            SafeArea(child: _NowPlaying(track: t, tint: tint)),
          ]);
        },
      ),
    );
  }
}

class _TopBar extends ConsumerWidget {
  const _TopBar({required this.track, required this.onQueue});
  final Track? track;
  final VoidCallback? onQueue;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = track;
    final label = t == null
        ? ''
        : t.isPreview
            ? 'PREVIEW'
            : t.isEpisode
                ? 'PODCAST'
                : 'NOW PLAYING';
    final canJam = t != null && onQueue != null;
    final inJam = ref.watch(jamProvider).jam != null;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
      child: Row(children: [
        GlassIconButton(icon: Icons.keyboard_arrow_down_rounded, tooltip: 'Close', onPressed: () => Navigator.pop(context)),
        if (canJam) const SizedBox(width: 50), // keeps the title centred
        Expanded(
          child: Column(children: [
            GradientMask(child: Text(label, style: const TextStyle(letterSpacing: 3, fontSize: 11, fontWeight: FontWeight.w800))),
            if (t?.album != null && !(t?.isPreview ?? false))
              Padding(
                padding: const EdgeInsets.only(top: 2),
                child: Text(t!.album!, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600)),
              ),
          ]),
        ),
        if (canJam) ...[
          GlassIconButton(
            icon: Icons.groups_rounded,
            tooltip: inJam ? 'Your Jam' : 'Start a Jam',
            color: inJam ? glowColor : null,
            onPressed: () => showJamSheet(context, ref),
          ),
          const SizedBox(width: 8),
        ],
        if (onQueue != null)
          GlassIconButton(icon: Icons.queue_music_rounded, tooltip: 'Queue', onPressed: onQueue)
        else if (t != null)
          GlassIconButton(icon: Icons.more_horiz_rounded, tooltip: 'More', onPressed: () => showTrackMenu(context, ref, t))
        else
          const SizedBox(width: 42),
      ]),
    );
  }
}

class _NowPlaying extends ConsumerWidget {
  const _NowPlaying({required this.track, required this.tint});
  final Track track;
  final Color? tint;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final c = ref.watch(playerProvider);
    final p = c.player;
    final t = track;
    final downloaded = ref.watch(offlineProvider).downloads.containsKey(t.id);
    final inJam = ref.watch(jamProvider).jam != null;
    return StreamBuilder<PlayerState>(
      stream: p.playerStateStream,
      initialData: p.playerState,
      builder: (context, s) {
        final playing = s.data?.playing == true;
        final loading = s.data?.processingState == ProcessingState.loading || s.data?.processingState == ProcessingState.buffering;
        return LayoutBuilder(builder: (context, box) {
          final art = min(box.maxWidth - 56, box.maxHeight * 0.44).clamp(140.0, 520.0);
          return Column(children: [
            _TopBar(track: t, onQueue: t.isPreview ? null : () => _showQueue(context, ref)),
            const JamBanner(),
            const Spacer(),
            Hero(
              tag: 'now-art',
              flightShuttleBuilder: (context, anim, dir, from, to) => AnimatedBuilder(
                animation: anim,
                builder: (context, _) => LayoutBuilder(
                  builder: (context, b) => Cover(t.coverUrl, size: b.maxWidth, radius: lerpDouble(b.maxWidth / 2, radiusL + 4, anim.value)!),
                ),
              ),
              child: _BreathingArt(url: t.coverUrl, size: art, playing: playing, tint: tint),
            ),
            const Spacer(),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 28),
              child: Row(children: [
                Expanded(
                  child: AnimatedSwitcher(
                    duration: const Duration(milliseconds: 350),
                    transitionBuilder: (child, a) => FadeTransition(
                      opacity: a,
                      child: SlideTransition(position: Tween(begin: const Offset(0.08, 0), end: Offset.zero).animate(a), child: child),
                    ),
                    child: Column(
                      key: ValueKey(t.id),
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(t.title, maxLines: 2, overflow: TextOverflow.ellipsis, style: Theme.of(context).textTheme.headlineMedium),
                        const SizedBox(height: 4),
                        Row(children: [
                          if (downloaded)
                            const Padding(padding: EdgeInsets.only(right: 6), child: Icon(Icons.download_done_rounded, size: 16, color: glowColor)),
                          Flexible(
                            child: Semantics(
                              button: !t.isEpisode && !t.isPreview && t.artists.isNotEmpty,
                              label: 'Open the artist',
                              child: GestureDetector(
                                behavior: HitTestBehavior.opaque,
                                onTap: t.isEpisode || t.isPreview || t.artists.isEmpty ? null : () => openArtistOf(context, t),
                                child: Row(mainAxisSize: MainAxisSize.min, children: [
                                  Flexible(
                                    child: Text(t.artistLine, maxLines: 1, overflow: TextOverflow.ellipsis,
                                        style: const TextStyle(color: Colors.white70, fontSize: 16)),
                                  ),
                                  if (!t.isEpisode && !t.isPreview && t.artists.isNotEmpty)
                                    const Icon(Icons.chevron_right_rounded, size: 18, color: Colors.white54),
                                ]),
                              ),
                            ),
                          ),
                        ]),
                      ],
                    ),
                  ),
                ),
                if (!t.isEpisode && !t.isPreview)
                  ValueListenableBuilder<Map<String, bool>>(
                    valueListenable: c.likes,
                    builder: (context, _, _) => LikeButton(liked: c.isLiked(t), onPressed: () => toggleLike(context, ref, t), size: 30),
                  ),
              ]),
            ),
            const SizedBox(height: 18),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 24),
              child: StreamBuilder<Duration>(
                stream: p.positionStream,
                builder: (context, ps) {
                  final total = p.duration ?? t.duration;
                  final pos = (ps.data ?? Duration.zero) > total ? total : (ps.data ?? Duration.zero);
                  return Column(children: [
                    SparkleBar(position: pos, duration: total, playing: playing, speed: p.speed, onSeek: c.seek),
                    Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
                      Text(formatDuration(pos), style: const TextStyle(color: muted, fontSize: 12)),
                      Text(t.isEpisode && total > pos ? '-${formatDuration(total - pos)}' : formatDuration(total),
                          style: const TextStyle(color: muted, fontSize: 12)),
                    ]),
                  ]);
                },
              ),
            ),
            const SizedBox(height: 10),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: Row(mainAxisAlignment: MainAxisAlignment.spaceEvenly, children: [
                if (t.isEpisode)
                  IconButton(iconSize: 32, tooltip: 'Back 10 seconds', icon: const Icon(Icons.replay_10_rounded), onPressed: () => c.skipBy(const Duration(seconds: -10)))
                else
                  StreamBuilder<bool>(
                    stream: p.shuffleModeEnabledStream,
                    builder: (_, sh) => IconButton(
                      tooltip: 'Shuffle',
                      icon: sh.data == true ? const GradientMask(child: Icon(Icons.shuffle_rounded)) : const Icon(Icons.shuffle_rounded, color: Colors.white70),
                      onPressed: t.isPreview || inJam ? null : c.toggleShuffle,
                    ),
                  ),
                IconButton(iconSize: 42, tooltip: 'Previous', icon: const Icon(Icons.skip_previous_rounded), onPressed: c.previous),
                AuroraPlayButton(playing: playing, loading: loading, onPressed: c.togglePlay, size: 78),
                IconButton(iconSize: 42, tooltip: 'Next', icon: const Icon(Icons.skip_next_rounded), onPressed: c.next),
                if (t.isEpisode)
                  IconButton(iconSize: 32, tooltip: 'Ahead 30 seconds', icon: const Icon(Icons.forward_30_rounded), onPressed: () => c.skipBy(const Duration(seconds: 30)))
                else
                  StreamBuilder<LoopMode>(
                    stream: p.loopModeStream,
                    builder: (_, lm) {
                      final mode = lm.data ?? LoopMode.off;
                      final icon = Icon(mode == LoopMode.one ? Icons.repeat_one_rounded : Icons.repeat_rounded, color: mode == LoopMode.off ? Colors.white70 : null);
                      return IconButton(tooltip: 'Repeat', icon: mode == LoopMode.off ? icon : GradientMask(child: icon), onPressed: t.isPreview || inJam ? null : c.cycleRepeat);
                    },
                  ),
              ]),
            ),
            const SizedBox(height: 14),
            Padding(
              padding: const EdgeInsets.fromLTRB(24, 0, 24, 16),
              child: t.isEpisode
                  ? inJam
                      ? const Text('Everyone in the Jam hears it at 1×', style: TextStyle(color: muted, fontSize: 12.5))
                      : _SpeedChip(controller: c)
                  : Row(children: [
                      Expanded(child: _UpNext(controller: c)),
                      if (!t.isPreview) ...[const SizedBox(width: 10), const _LyricsButton()],
                    ]),
            ),
          ]);
        });
      },
    );
  }

  void _showQueue(BuildContext context, WidgetRef ref) {
    final c = ref.read(playerProvider);
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      builder: (sheet) => DraggableScrollableSheet(
        expand: false,
        initialChildSize: 0.7,
        builder: (context, scroll) => StreamBuilder<List<Track>>(
          stream: c.queueChanges,
          initialData: c.queue,
          builder: (context, q) => StreamBuilder<Track?>(
            stream: c.currentStream,
            initialData: c.current,
            builder: (context, _) {
              final queue = q.data ?? const [];
              return ListView.builder(
                controller: scroll,
                itemCount: queue.length + 1,
                itemBuilder: (context, i) {
                  if (i == 0) {
                    return SectionTitle(c.inJam ? 'Jam queue' : 'Queue',
                        subtitle: c.inJam ? 'Everyone in the Jam shares this' : null, padding: const EdgeInsets.fromLTRB(20, 0, 20, 8));
                  }
                  final k = i - 1;
                  final current = k == c.currentIndex;
                  final title = Text(queue[k].title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w600));
                  return ListTile(
                    contentPadding: const EdgeInsets.symmetric(horizontal: 20),
                    leading: Cover(queue[k].thumbUrl, size: 44),
                    title: current ? GradientMask(child: title) : title,
                    subtitle: Text(queue[k].artistLine, maxLines: 1, overflow: TextOverflow.ellipsis),
                    trailing: current
                        ? StreamBuilder<bool>(stream: c.player.playingStream, builder: (_, s) => EqualizerBars(playing: s.data == true))
                        : c.inJam
                            ? null
                            : IconButton(icon: const Icon(Icons.close_rounded), onPressed: () => c.removeAt(k)),
                    onTap: () => c.jumpTo(k),
                  );
                },
              );
            },
          ),
        ),
      ),
    );
  }
}

/// The cover, breathing gently with a glow of its own colour while music plays.
class _BreathingArt extends StatefulWidget {
  const _BreathingArt({required this.url, required this.size, required this.playing, required this.tint});
  final String? url;
  final double size;
  final bool playing;
  final Color? tint;

  @override
  State<_BreathingArt> createState() => _BreathingArtState();
}

class _BreathingArtState extends State<_BreathingArt> with SingleTickerProviderStateMixin {
  late final _c = AnimationController(vsync: this, duration: const Duration(milliseconds: 3600));

  @override
  void initState() {
    super.initState();
    _sync();
  }

  @override
  void didUpdateWidget(_BreathingArt old) {
    super.didUpdateWidget(old);
    _sync();
  }

  void _sync() {
    if (widget.playing && !_c.isAnimating) {
      _c.repeat(reverse: true);
    } else if (!widget.playing && _c.isAnimating) {
      _c.stop();
    }
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final glow = widget.tint ?? accent;
    return AnimatedScale(
      scale: widget.playing ? 1.0 : 0.9,
      duration: const Duration(milliseconds: 600),
      curve: Curves.easeOutBack,
      child: AnimatedBuilder(
        animation: _c,
        builder: (context, child) {
          final v = Curves.easeInOut.transform(_c.value);
          return Transform.scale(
            scale: 1 + 0.015 * v,
            child: Container(
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(radiusL + 4),
                boxShadow: [
                  BoxShadow(color: glow.withValues(alpha: widget.playing ? 0.35 + 0.2 * v : 0.2), blurRadius: 40 + 30 * v, spreadRadius: 2),
                  const BoxShadow(color: Color(0x88000000), blurRadius: 24, offset: Offset(0, 14)),
                ],
              ),
              child: child,
            ),
          );
        },
        child: Cover(widget.url, size: widget.size, radius: radiusL + 4),
      ),
    );
  }
}

class _UpNext extends StatelessWidget {
  const _UpNext({required this.controller});
  final PlayerController controller;

  @override
  Widget build(BuildContext context) => StreamBuilder<Track?>(
        stream: controller.currentStream,
        initialData: controller.current,
        builder: (context, _) {
          final i = controller.currentIndex;
          final q = controller.queue;
          final next = i != null && i + 1 < q.length && !controller.player.shuffleModeEnabled ? q[i + 1] : null;
          return AnimatedOpacity(
            opacity: next == null ? 0 : 1,
            duration: const Duration(milliseconds: 300),
            child: Glass(
              blur: false,
              radius: 18,
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
              child: Row(children: [
                const Text('UP NEXT', style: TextStyle(fontSize: 10.5, letterSpacing: 2, color: muted, fontWeight: FontWeight.w800)),
                const SizedBox(width: 12),
                Expanded(
                  child: Text(next == null ? '' : '${next.title} · ${next.artistLine}', maxLines: 1, overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontSize: 13)),
                ),
              ]),
            ),
          );
        },
      );
}

class _LyricsButton extends StatelessWidget {
  const _LyricsButton();

  @override
  Widget build(BuildContext context) => Pressable(
        onTap: () => Navigator.of(context).push(LyricsScreen.route()),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(18),
            gradient: LinearGradient(colors: [accent.withValues(alpha: 0.45), accent2.withValues(alpha: 0.35)]),
            border: Border.all(color: Colors.white.withValues(alpha: 0.15)),
          ),
          child: const Row(mainAxisSize: MainAxisSize.min, children: [
            Icon(Icons.lyrics_rounded, size: 18),
            SizedBox(width: 6),
            Text('Lyrics', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 13)),
          ]),
        ),
      );
}

class _SpeedChip extends StatelessWidget {
  const _SpeedChip({required this.controller});
  final PlayerController controller;

  static const _speeds = [0.8, 1.0, 1.2, 1.5, 1.8, 2.0];

  @override
  Widget build(BuildContext context) => StreamBuilder<double>(
        stream: controller.player.speedStream,
        initialData: controller.player.speed,
        builder: (context, s) {
          final speed = s.data ?? 1.0;
          return Row(mainAxisAlignment: MainAxisAlignment.center, children: [
            for (final v in _speeds)
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 3),
                child: Pressable(
                  onTap: () => controller.setPodcastSpeed(v),
                  child: AnimatedContainer(
                    duration: const Duration(milliseconds: 250),
                    padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(14),
                      gradient: (speed - v).abs() < 0.01 ? auroraSoft : null,
                      border: Border.all(color: glassBorder),
                    ),
                    child: Text('${v == v.roundToDouble() ? v.toStringAsFixed(0) : v}×',
                        style: const TextStyle(fontSize: 12.5, fontWeight: FontWeight.w700)),
                  ),
                ),
              ),
          ]);
        },
      );
}
