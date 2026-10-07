import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:just_audio/just_audio.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../state/data.dart';
import '../state/player.dart';
import '../state/session.dart';
import 'fx/colors.dart';
import 'fx/motion.dart';
import 'fx/sparkle.dart';
import 'theme.dart';
import 'tracklist_screen.dart';
import 'widgets.dart';

const _clip = Duration(seconds: 30);

/// Tap a recommendation (or a song from search or a chart): hear 30 seconds of it, then like
/// it, which also downloads it to your server. Whatever was playing comes back afterwards.
Future<void> showPreview(BuildContext context, WidgetRef ref, {CatalogItem? item, Track? track, String? reason}) async {
  if (item == null && track == null) return;
  final nav = Navigator.of(context);
  await showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useRootNavigator: true,
    builder: (_) => _PreviewSheet(item: item, track: track, reason: reason, openArtist: (a) => nav.push(MaterialPageRoute(builder: (_) => ArtistScreen(a)))),
  );
  await ref.read(playerProvider).endPreview();
}

class _PreviewSheet extends ConsumerStatefulWidget {
  const _PreviewSheet({this.item, this.track, this.reason, required this.openArtist});
  final CatalogItem? item;
  final Track? track;
  final String? reason;
  final void Function(String artist) openArtist;

  @override
  ConsumerState<_PreviewSheet> createState() => _PreviewSheetState();
}

class _PreviewSheetState extends ConsumerState<_PreviewSheet> {
  Duration _start = Duration.zero;
  bool _noPreview = false;
  bool _liked = false;
  bool _requested = false;
  bool _busy = false;
  StreamSubscription? _errors;
  StreamSubscription? _positions;

  Track? get _serverTrack => widget.track;
  bool get _onServer => _serverTrack != null || widget.item?.inLibrary != null;
  bool get _playableNow => _serverTrack?.playable == true;
  String get _title => _serverTrack?.title ?? widget.item!.title;
  List<String> get _artists => _serverTrack?.artists ?? widget.item!.artists;
  String? get _cover => _serverTrack?.coverUrl ?? widget.item?.coverUrl;

  @override
  void initState() {
    super.initState();
    final player = ref.read(playerProvider);
    _liked = _serverTrack != null && player.isLiked(_serverTrack!);
    _errors = player.player.errorStream.listen((_) {
      if (mounted) setState(() => _noPreview = true);
    });
    _positions = player.player.positionStream.listen((p) {
      if (player.previewing && p - _start >= _clip && player.player.playing) player.player.pause();
    });
    WidgetsBinding.instance.addPostFrameCallback((_) => _startPreview());
  }

  Future<void> _startPreview() async {
    final player = ref.read(playerProvider);
    final api = ref.read(apiProvider);
    try {
      final t = _serverTrack;
      if (t != null && t.playable) {
        // a song on the server: play a stretch from about a third of the way in
        _start = t.duration > const Duration(seconds: 90) ? t.duration * 0.3 : Duration.zero;
        await player.preview(t, start: _start);
      } else if (widget.item != null && api != null) {
        _start = Duration.zero;
        await player.preview(widget.item!.previewTrack(api.previewUri(widget.item!)));
      }
    } catch (_) {
      if (mounted) setState(() => _noPreview = true);
    }
  }

  @override
  void dispose() {
    _errors?.cancel();
    _positions?.cancel();
    super.dispose();
  }

  Future<void> _like() async {
    final api = ref.read(apiProvider);
    if (api == null || _busy) return;
    final t = _serverTrack;
    if (t != null) {
      setState(() => _liked = !_liked);
      await toggleLike(context, ref, t);
      if (mounted) setState(() => _liked = ref.read(playerProvider).isLiked(t));
      return;
    }
    if (_liked) return; // liking again from here does nothing; unlike it in the library
    setState(() {
      _busy = true;
      _liked = true;
    });
    try {
      final got = await api.request(widget.item!, like: true);
      ref.read(playerProvider).likes.value = {...ref.read(playerProvider).likes.value, got.id: true};
      setState(() => _requested = true);
      if (mounted) toast(context, got.playable ? 'Liked' : 'Liked · your server is downloading it');
      ref.refreshLibrary();
    } on ApiException catch (e) {
      setState(() => _liked = false);
      if (mounted) toast(context, e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _justDownload() async {
    final api = ref.read(apiProvider);
    if (api == null || _busy) return;
    setState(() => _busy = true);
    try {
      await api.request(widget.item!);
      setState(() => _requested = true);
      if (mounted) toast(context, 'Added · your server is downloading it');
      ref.invalidate(requestsProvider);
      ref.invalidate(homeProvider);
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _playFull() {
    final t = _serverTrack;
    if (t == null) return;
    ref.read(playerProvider).play([t]);
    Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    final player = ref.watch(playerProvider);
    final cover = _cover;
    final tint = cover == null ? null : ref.watch(artColorProvider(cover)).value;
    final size = MediaQuery.sizeOf(context);
    final art = (size.width * 0.62).clamp(160.0, 280.0);
    return SafeArea(
      child: Stack(children: [
        const Positioned.fill(child: AmbientSparkles(perSecond: 4)),
        SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(24, 0, 24, 20),
          child: Column(children: [
            GradientMask(
              child: Text(_playableNow ? 'LISTEN' : 'PREVIEW',
                  style: const TextStyle(letterSpacing: 4, fontSize: 12, fontWeight: FontWeight.w800)),
            ),
            const SizedBox(height: 16),
            StreamBuilder<PlayerState>(
              stream: player.player.playerStateStream,
              initialData: player.player.playerState,
              builder: (context, s) {
                final playing = s.data?.playing == true && player.previewing;
                final loading = s.data?.processingState == ProcessingState.loading || s.data?.processingState == ProcessingState.buffering;
                return Pressable(
                  onTap: _noPreview ? null : player.togglePlay,
                  child: AnimatedScale(
                    scale: playing ? 1.0 : 0.94,
                    duration: const Duration(milliseconds: 500),
                    curve: Curves.easeOutBack,
                    child: Container(
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(radiusL),
                        boxShadow: [BoxShadow(color: (tint ?? accent).withValues(alpha: playing ? 0.55 : 0.3), blurRadius: playing ? 50 : 30)],
                      ),
                      child: Stack(alignment: Alignment.center, children: [
                        Cover(cover, size: art, radius: radiusL),
                        AnimatedOpacity(
                          opacity: playing && !loading ? 0 : 1,
                          duration: const Duration(milliseconds: 250),
                          child: Glass(
                            radius: 36,
                            child: SizedBox(
                              width: 72,
                              height: 72,
                              child: loading && !_noPreview
                                  ? const Padding(padding: EdgeInsets.all(22), child: CircularProgressIndicator(strokeWidth: 2.5, color: Colors.white))
                                  : Icon(_noPreview ? Icons.music_off_rounded : Icons.play_arrow_rounded, size: 40),
                            ),
                          ),
                        ),
                      ]),
                    ),
                  ),
                );
              },
            ),
            const SizedBox(height: 22),
            Text(_title, textAlign: TextAlign.center, maxLines: 2, overflow: TextOverflow.ellipsis,
                style: Theme.of(context).textTheme.headlineMedium),
            const SizedBox(height: 4),
            GestureDetector(
              onTap: _artists.isEmpty
                  ? null
                  : () {
                      Navigator.pop(context);
                      widget.openArtist(_artists.first);
                    },
              child: Text(_artists.join(', '), textAlign: TextAlign.center, style: const TextStyle(color: muted, fontSize: 16)),
            ),
            if (widget.reason != null) ...[const SizedBox(height: 12), ReasonChip(widget.reason!)],
            const SizedBox(height: 18),
            if (_noPreview)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 8),
                child: Text('No preview is available for this song.', style: TextStyle(color: muted)),
              )
            else
              StreamBuilder<Duration>(
                stream: player.player.positionStream,
                builder: (context, p) {
                  final heard = (p.data ?? Duration.zero) - _start;
                  final clip = _clip < (player.player.duration ?? _clip) ? _clip : (player.player.duration ?? _clip);
                  return Column(children: [
                    SparkleBar(
                      position: player.previewing ? (heard < Duration.zero ? Duration.zero : heard) : Duration.zero,
                      duration: clip,
                      playing: player.player.playing && player.previewing,
                      height: 22,
                      trackHeight: 3,
                      sparklesPerSecond: 9,
                    ),
                    Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
                      Text(formatDuration(heard < Duration.zero || !player.previewing ? Duration.zero : (heard > clip ? clip : heard)),
                          style: const TextStyle(color: muted, fontSize: 12)),
                      Text(formatDuration(clip), style: const TextStyle(color: muted, fontSize: 12)),
                    ]),
                  ]);
                },
              ),
            const SizedBox(height: 18),
            Row(mainAxisAlignment: MainAxisAlignment.center, children: [
              Column(children: [
                LikeButton(liked: _liked, onPressed: _like, size: 38),
                const SizedBox(height: 2),
                Text(
                  _liked ? (_requested ? 'Liked · downloading' : 'Liked') : (_onServer ? 'Like' : 'Like & download'),
                  style: const TextStyle(fontSize: 12.5, color: Colors.white70),
                ),
              ]),
              if (_playableNow) ...[
                const SizedBox(width: 28),
                Column(children: [
                  AuroraPlayButton(playing: false, onPressed: _playFull, size: 56),
                  const SizedBox(height: 8),
                  const Text('Play song', style: TextStyle(fontSize: 12.5, color: Colors.white70)),
                ]),
              ],
            ]),
            if (!_onServer && !_liked && !_requested)
              TextButton(onPressed: _busy ? null : _justDownload, child: const Text('Just download it (don\'t like)')),
            if (_onServer && !_playableNow)
              const Padding(
                padding: EdgeInsets.only(top: 8),
                child: Text('Your server is downloading this song.', style: TextStyle(color: muted, fontSize: 13)),
              ),
          ]),
        ),
      ]),
    );
  }
}
