import 'dart:ui' show ImageFilter;

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/models.dart';
import '../state/data.dart';
import '../state/offline.dart';
import '../state/player.dart';
import '../state/session.dart';
import 'artist_about.dart';
import 'fx/colors.dart';
import 'fx/motion.dart';
import 'fx/sparkle.dart';
import 'reorder_songs_screen.dart';
import 'search_screen.dart';
import 'theme.dart';
import 'widgets.dart';

enum _Kind { playlist, album, liked, downloads }

/// A list of songs with a header, Play / Shuffle and offline download: used for playlists,
/// albums, Liked Songs and Downloads.
class TrackListScreen extends ConsumerWidget {
  const TrackListScreen.playlist(this.id, this.title, {super.key}) : _kind = _Kind.playlist;
  const TrackListScreen.album(this.id, this.title, {super.key}) : _kind = _Kind.album;
  const TrackListScreen.liked({super.key})
      : id = '',
        title = 'Liked Songs',
        _kind = _Kind.liked;
  const TrackListScreen.downloads({super.key})
      : id = '',
        title = 'Downloads',
        _kind = _Kind.downloads;

  final String id;
  final String title;
  final _Kind _kind;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final offline = ref.watch(offlineProvider);
    final AsyncValue<TrackPage> value = switch (_kind) {
      _Kind.playlist => ref.watch(playlistProvider(id)),
      _Kind.album => ref.watch(albumProvider(id)),
      _Kind.liked => ref.watch(likedProvider),
      _Kind.downloads => AsyncData(TrackPage(total: offline.downloads.length, tracks: offline.downloads.values.toList().reversed.toList())),
    };
    void refresh() => switch (_kind) {
          _Kind.playlist => ref.invalidate(playlistProvider(id)),
          _Kind.album => ref.invalidate(albumProvider(id)),
          _Kind.liked => ref.invalidate(likedProvider),
          _Kind.downloads => null,
        };
    final page = value.value;
    final editable = _kind == _Kind.playlist && page != null && page.editable;
    return Scaffold(
      body: Stack(children: [
        RefreshIndicator(
          color: accent,
          backgroundColor: surface,
          onRefresh: () async => refresh(),
          child: value.when(
            data: (page) => _body(context, ref, page, offline),
            loading: () => ListView(children: [_header(context, ref, null, offline), const ListSkeleton()]),
            error: (e, _) => ListView(children: [_header(context, ref, null, offline), ErrorView(e, onRetry: refresh)]),
          ),
        ),
        _BackButton(
          actions: [
            if (_kind == _Kind.liked && page != null && page.tracks.length > 1)
              PopupMenuButton<String>(
                icon: const Icon(Icons.more_horiz_rounded),
                color: surface,
                onSelected: (_) => push(context, ReorderSongsScreen.liked(tracks: page.tracks)),
                itemBuilder: (_) => const [PopupMenuItem(value: 'reorder', child: Text('Reorder songs'))],
              ),
            if (editable)
              PopupMenuButton<String>(
                icon: const Icon(Icons.more_horiz_rounded),
                color: surface,
                onSelected: (v) => v == 'reorder'
                    ? push(context, ReorderSongsScreen(playlistId: id, name: page.name.isNotEmpty ? page.name : title, tracks: page.tracks))
                    : _editPlaylist(context, ref, v, page.name.isNotEmpty ? page.name : title),
                itemBuilder: (_) => [
                  if (page.tracks.length > 1) const PopupMenuItem(value: 'reorder', child: Text('Reorder songs')),
                  const PopupMenuItem(value: 'rename', child: Text('Rename')),
                  const PopupMenuItem(value: 'delete', child: Text('Delete playlist')),
                ],
              ),
          ],
        ),
      ]),
    );
  }

  Widget _header(BuildContext context, WidgetRef ref, TrackPage? page, OfflineState offline) {
    final tracks = page?.tracks ?? const <Track>[];
    final cover = page?.imageUrl ?? (tracks.isNotEmpty ? tracks.first.coverUrl : null);
    final name = page != null && page.name.isNotEmpty ? page.name : title;
    final tint = cover == null ? null : ref.watch(artColorProvider(cover)).value;
    final total = tracks.fold<Duration>(Duration.zero, (s, t) => s + t.duration);
    final playable = tracks.where((t) => t.playable).toList();
    final player = ref.read(playerProvider);
    final allDownloaded = playable.isNotEmpty && playable.every((t) => offline.downloads.containsKey(t.id));
    final waiting = tracks.length - playable.length;
    final info = page == null
        ? ''
        : _kind == _Kind.downloads
            ? '${tracks.length} songs · ${formatBytes(offline.downloadBytes)}'
            : '${page.total} songs · ${formatLength(total)}${waiting > 0 ? ' · $waiting still downloading to server' : ''}';
    final art = switch (_kind) {
      _Kind.liked => const LikedCover(size: 170, radius: 85),
      _Kind.downloads => Container(
          width: 170,
          height: 170,
          decoration: BoxDecoration(shape: BoxShape.circle, border: Border.all(color: glassBorder), color: glassFill),
          child: const GradientMask(child: Icon(Icons.download_done_rounded, size: 80)),
        ),
      _ => Container(
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(radiusL),
            boxShadow: [BoxShadow(color: (tint ?? accent).withValues(alpha: 0.45), blurRadius: 50, spreadRadius: 2)],
          ),
          child: Cover(cover, size: 200, radius: radiusL, icon: _kind == _Kind.album ? Icons.album_rounded : Icons.queue_music_rounded),
        ),
    };
    return SizedBox(
      height: 430 + MediaQuery.paddingOf(context).top,
      child: Stack(children: [
        if (cover != null && _kind != _Kind.liked && _kind != _Kind.downloads)
          Positioned.fill(
            child: ShaderMask(
              blendMode: BlendMode.dstIn,
              shaderCallback: (r) => const LinearGradient(
                begin: Alignment.topCenter,
                end: Alignment.bottomCenter,
                colors: [Colors.white, Colors.transparent],
              ).createShader(r),
              child: Opacity(
                opacity: 0.45,
                child: ImageFiltered(
                  imageFilter: ImageFilter.blur(sigmaX: 40, sigmaY: 40),
                  child: CachedNetworkImage(imageUrl: cover, fit: BoxFit.cover, memCacheWidth: 200, errorWidget: (_, _, _) => const SizedBox()),
                ),
              ),
            ),
          ),
        if (_kind == _Kind.liked) ...[
          Positioned.fill(
            child: DecoratedBox(
              decoration: BoxDecoration(
                gradient: RadialGradient(
                  center: const Alignment(0, -0.25),
                  radius: 0.8,
                  colors: [Colors.white.withValues(alpha: 0.10), Colors.transparent],
                ),
              ),
            ),
          ),
          const Positioned.fill(child: AmbientSparkles(perSecond: 7, colors: [Colors.white, glowColor, Color(0xFFF9A8D4)])),
        ],
        Positioned.fill(
          child: SafeArea(
            bottom: false,
            child: Column(children: [
              const SizedBox(height: 52),
              FadeSlideIn(child: _Floating(child: art)),
              const SizedBox(height: 22),
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 24),
                child: Text(name, textAlign: TextAlign.center, maxLines: 2, overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.headlineMedium),
              ),
              const SizedBox(height: 4),
              Text(info, style: const TextStyle(color: muted, fontSize: 13)),
              const Spacer(),
              Padding(
                padding: const EdgeInsets.fromLTRB(24, 0, 24, 12),
                child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                  if (_kind != _Kind.downloads && playable.isNotEmpty)
                    GlassIconButton(
                      tooltip: allDownloaded ? 'Remove downloads' : 'Download to this device',
                      icon: allDownloaded ? Icons.download_done_rounded : Icons.download_rounded,
                      color: allDownloaded ? glowColor : null,
                      onPressed: () async {
                        final store = ref.read(offlineProvider.notifier);
                        if (allDownloaded) {
                          await store.remove([for (final t in playable) t.id]);
                        } else {
                          final n = await store.download(playable);
                          if (context.mounted) toast(context, n == 0 ? 'Already downloaded' : 'Downloading $n songs');
                        }
                      },
                    ),
                  const SizedBox(width: 18),
                  AuroraPlayButton(playing: false, size: 64, onPressed: tracks.isEmpty ? null : () => player.play(tracks)),
                  const SizedBox(width: 18),
                  GlassIconButton(
                    tooltip: 'Shuffle',
                    icon: Icons.shuffle_rounded,
                    onPressed: tracks.isEmpty ? null : () => player.play(tracks, shuffle: true),
                  ),
                ]),
              ),
            ]),
          ),
        ),
      ]),
    );
  }

  Widget _body(BuildContext context, WidgetRef ref, TrackPage page, OfflineState offline) {
    final tracks = page.tracks;
    final player = ref.read(playerProvider);
    final editable = _kind == _Kind.playlist && page.editable;
    return CustomScrollView(slivers: [
      SliverToBoxAdapter(child: _header(context, ref, page, offline)),
      if (tracks.isEmpty)
        SliverToBoxAdapter(
          child: Padding(
            padding: const EdgeInsets.all(32),
            child: Text(
              _kind == _Kind.downloads ? 'Songs you download play without internet. Use the download button on any playlist or album.' : 'No songs yet.',
              textAlign: TextAlign.center,
              style: const TextStyle(color: muted),
            ),
          ),
        ),
      SliverList.builder(
        itemCount: tracks.length,
        itemBuilder: (context, i) {
          final tile = TrackTile(
            track: tracks[i],
            number: tracks[i].trackNumber ?? i + 1,
            showCover: _kind != _Kind.album,
            playlistId: editable ? id : null,
            position: editable || _kind == _Kind.liked ? i : null,
            inLiked: _kind == _Kind.liked,
            list: tracks,
            onTap: () => player.play(tracks, index: i),
          );
          return i < 14 ? FadeSlideIn(index: i, child: tile) : tile;
        },
      ),
      const SliverToBoxAdapter(child: SizedBox(height: 190)),
    ]);
  }

  Future<void> _editPlaylist(BuildContext context, WidgetRef ref, String action, String name) async {
    final api = ref.read(apiProvider);
    if (api == null) return;
    if (action == 'rename') {
      final n = await askText(context, 'Rename playlist', 'Name', initial: name);
      if (n == null || n.trim().isEmpty) return;
      await api.renamePlaylist(id, n.trim());
    } else {
      final ok = await showDialog<bool>(
        context: context,
        builder: (d) => AlertDialog(
          title: Text('Delete "$name"?'),
          content: const Text('The songs stay in your library.'),
          actions: [
            TextButton(onPressed: () => Navigator.pop(d, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(d, true), child: const Text('Delete')),
          ],
        ),
      );
      if (ok != true) return;
      await api.deletePlaylist(id);
      if (context.mounted) Navigator.pop(context);
    }
    ref.refreshLibrary();
  }
}

/// A gentle up-and-down float for header art.
class _Floating extends StatefulWidget {
  const _Floating({required this.child});
  final Widget child;
  @override
  State<_Floating> createState() => _FloatingState();
}

class _FloatingState extends State<_Floating> with SingleTickerProviderStateMixin {
  late final _c = AnimationController(vsync: this, duration: const Duration(seconds: 4));

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (!MediaQuery.disableAnimationsOf(context) && !_c.isAnimating) _c.repeat(reverse: true);
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
        animation: _c,
        child: widget.child,
        builder: (context, child) => Transform.translate(offset: Offset(0, -5 * Curves.easeInOut.transform(_c.value)), child: child),
      );
}

/// Back (and page actions) floating over the header, always in reach.
class _BackButton extends StatelessWidget {
  const _BackButton({this.actions = const []});
  final List<Widget> actions;

  @override
  Widget build(BuildContext context) => Positioned(
        left: 16,
        right: 16,
        top: MediaQuery.paddingOf(context).top + 8,
        child: Row(children: [
          if (Navigator.of(context).canPop())
            GlassIconButton(icon: Icons.arrow_back_rounded, tooltip: 'Back', onPressed: () => Navigator.maybePop(context)),
          const Spacer(),
          for (final a in actions) Glass(radius: 21, blur: false, child: a),
        ]),
      );
}

/// Follow an artist: their new releases show on Home and ring the bell (and the phone).
class FollowButton extends ConsumerStatefulWidget {
  const FollowButton({super.key, required this.name, required this.deezerId, required this.following});
  final String name;
  final int deezerId;
  final bool following;

  @override
  ConsumerState<FollowButton> createState() => _FollowButtonState();
}

class _FollowButtonState extends ConsumerState<FollowButton> {
  bool? _following; // what was just tapped, until the page reloads
  bool _busy = false;

  Future<void> _toggle() async {
    final api = ref.read(apiProvider);
    if (api == null || _busy) return;
    final follow = !(_following ?? widget.following);
    setState(() {
      _busy = true;
      _following = follow;
    });
    try {
      if (follow) {
        await api.followArtist(widget.name, widget.deezerId);
      } else {
        await api.unfollowArtist(widget.deezerId);
      }
      HapticFeedback.lightImpact();
      if (mounted) {
        toast(context, follow ? "Following ${widget.name}: you'll hear about new releases" : 'Unfollowed ${widget.name}');
      }
      ref.invalidate(followingProvider);
      ref.invalidate(homeProvider);
    } catch (e) {
      if (mounted) {
        setState(() => _following = !follow);
        toast(context, '$e');
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final following = _following ?? widget.following;
    return Pressable(
      onTap: _toggle,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 200),
        padding: const EdgeInsets.symmetric(horizontal: 22, vertical: 9),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(22),
          gradient: following ? null : aurora,
          border: following ? Border.all(color: Colors.white.withValues(alpha: 0.45)) : null,
        ),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Icon(following ? Icons.notifications_active_rounded : Icons.person_add_alt_1_rounded, size: 18),
          const SizedBox(width: 8),
          Text(following ? 'Following' : 'Follow', style: const TextStyle(fontWeight: FontWeight.w800)),
        ]),
      ),
    );
  }
}

class ArtistScreen extends ConsumerWidget {
  const ArtistScreen(this.name, {super.key});
  final String name;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final value = ref.watch(artistProvider(name));
    final player = ref.read(playerProvider);
    final photo = value.value?.about?.imageUrl;
    final tint = photo == null ? null : ref.watch(artColorProvider(photo)).value;
    return Scaffold(
      body: Stack(children: [
        CustomScrollView(slivers: [
          SliverToBoxAdapter(
            // as tall as what's in it (a long name, Follow, the play row), and never shorter than before
            child: ConstrainedBox(
              constraints: BoxConstraints(minHeight: 400 + MediaQuery.paddingOf(context).top),
              child: Stack(children: [
                if (photo != null)
                  Positioned.fill(
                    child: ShaderMask(
                      blendMode: BlendMode.dstIn,
                      shaderCallback: (r) => const LinearGradient(
                        begin: Alignment.topCenter,
                        end: Alignment.bottomCenter,
                        colors: [Colors.white, Colors.transparent],
                      ).createShader(r),
                      child: Opacity(
                        opacity: 0.4,
                        child: ImageFiltered(
                          imageFilter: ImageFilter.blur(sigmaX: 30, sigmaY: 30),
                          child: CachedNetworkImage(imageUrl: photo, fit: BoxFit.cover, memCacheWidth: 300, errorWidget: (_, _, _) => const SizedBox()),
                        ),
                      ),
                    ),
                  ),
                SafeArea(
                  bottom: false,
                  child: SizedBox(
                    width: double.infinity,
                    child: Column(mainAxisSize: MainAxisSize.min, children: [
                      const SizedBox(height: 56),
                      FadeSlideIn(
                        child: Container(
                          decoration: BoxDecoration(
                            shape: BoxShape.circle,
                            boxShadow: [BoxShadow(color: (tint ?? accent).withValues(alpha: 0.45), blurRadius: 60)],
                          ),
                          child: ArtistAvatar(photo, size: 180),
                        ),
                      ),
                      const SizedBox(height: 18),
                      Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 24),
                        child: Text(name, textAlign: TextAlign.center, style: Theme.of(context).textTheme.headlineLarge),
                      ),
                      const SizedBox(height: 6),
                      Text(
                        value.whenOrNull(
                              data: (a) => [
                                '${a.tracks.length} song${a.tracks.length == 1 ? '' : 's'} on your server',
                                if (a.about?.fans != null && a.about!.fans! > 0) '${_compact(a.about!.fans!)} fans',
                              ].join(' · '),
                            ) ??
                            '',
                        style: const TextStyle(color: muted),
                      ),
                      const SizedBox(height: 14),
                      if (value.value?.deezerId != null) FollowButton(name: name, deezerId: value.value!.deezerId!, following: value.value!.following),
                      if (value.value?.tracks.isNotEmpty == true)
                        Padding(
                          padding: const EdgeInsets.only(top: 22, bottom: 8),
                          child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                            AuroraPlayButton(playing: false, size: 60, onPressed: () => player.play(value.value!.tracks)),
                            const SizedBox(width: 18),
                            GlassIconButton(icon: Icons.shuffle_rounded, tooltip: 'Shuffle', onPressed: () => player.play(value.value!.tracks, shuffle: true)),
                          ]),
                        ),
                    ]),
                  ),
                ),
              ]),
            ),
          ),
          ...value.when(
            loading: () => [const SliverToBoxAdapter(child: ListSkeleton(rows: 6))],
            error: (e, _) => [SliverToBoxAdapter(child: ErrorView(e, onRetry: () => ref.invalidate(artistProvider(name))))],
            data: (a) => _sections(context, ref, a),
          ),
          const SliverToBoxAdapter(child: SizedBox(height: 190)),
        ]),
        const _BackButton(),
      ]),
    );
  }

  static String _compact(int n) => n >= 1000000 ? '${(n / 1000000).toStringAsFixed(1)}M' : n >= 1000 ? '${(n / 1000).round()}K' : '$n';

  List<Widget> _sections(BuildContext context, WidgetRef ref, ArtistPage a) {
    final player = ref.read(playerProvider);
    final tracks = a.tracks;
    return [
      SliverToBoxAdapter(child: ArtistAboutSection(name)),
      if (a.popular.isNotEmpty) ...[
        const SliverToBoxAdapter(child: SectionTitle('Popular', subtitle: 'Tap a song to preview it')),
        SliverList.builder(
          itemCount: a.popular.length,
          itemBuilder: (context, i) => FadeSlideIn(index: i, child: CatalogTile(item: a.popular[i], rank: i + 1)),
        ),
      ],
      if (tracks.isNotEmpty) ...[
        SliverToBoxAdapter(child: SectionTitle('On your server', subtitle: '${tracks.length} songs')),
        SliverList.builder(
          itemCount: tracks.length,
          itemBuilder: (context, i) => TrackTile(track: tracks[i], list: tracks, onTap: () => player.play(tracks, index: i)),
        ),
      ],
      if (a.albums.isNotEmpty) ...[
        const SliverToBoxAdapter(child: SectionTitle('Albums')),
        SliverToBoxAdapter(
          child: SizedBox(
            height: 206,
            child: ListView.separated(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 20),
              itemCount: a.albums.length,
              separatorBuilder: (_, _) => const SizedBox(width: 14),
              itemBuilder: (context, i) => Pressable(
                onTap: () => push(context, TrackListScreen.album(a.albums[i].id, a.albums[i].name)),
                child: SizedBox(
                  width: 140,
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Cover(a.albums[i].coverUrl, size: 140, radius: radiusM, icon: Icons.album_rounded),
                    const SizedBox(height: 8),
                    Text(a.albums[i].name, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700)),
                    Text(a.albums[i].year, style: const TextStyle(color: muted, fontSize: 12)),
                  ]),
                ),
              ),
            ),
          ),
        ),
      ],
      if (a.related.isNotEmpty) ...[
        const SliverToBoxAdapter(child: SectionTitle('Fans also like')),
        SliverToBoxAdapter(
          child: SizedBox(
            height: 160,
            child: ListView.separated(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 20),
              itemCount: a.related.length,
              separatorBuilder: (_, _) => const SizedBox(width: 14),
              itemBuilder: (context, i) => Pressable(
                onTap: () => push(context, ArtistScreen(a.related[i].name)),
                child: SizedBox(
                  width: 108,
                  child: Column(children: [
                    ArtistAvatar(a.related[i].thumbUrl, size: 100),
                    const SizedBox(height: 8),
                    Text(a.related[i].name, maxLines: 1, overflow: TextOverflow.ellipsis, textAlign: TextAlign.center,
                        style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13)),
                  ]),
                ),
              ),
            ),
          ),
        ),
      ],
    ];
  }
}
