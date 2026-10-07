import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../state/brand.dart';
import '../state/data.dart';
import '../state/player.dart';
import '../state/session.dart';
import '../state/updates.dart';
import 'catalog_album_screen.dart';
import 'notifications_screen.dart';
import 'fx/motion.dart';
import 'fx/sparkle.dart';
import 'podcast_screen.dart';
import 'preview_sheet.dart';
import 'settings_screen.dart';
import 'theme.dart';
import 'tracklist_screen.dart';
import 'widgets.dart';

class HomeScreen extends ConsumerStatefulWidget {
  const HomeScreen({super.key});
  @override
  ConsumerState<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends ConsumerState<HomeScreen> {
  Timer? _waiting;

  @override
  void dispose() {
    _waiting?.cancel();
    super.dispose();
  }

  String _greeting() {
    final h = DateTime.now().hour;
    return h < 5 ? 'Good night' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
  }

  @override
  Widget build(BuildContext context) {
    final home = ref.watch(homeProvider);
    final name = ref.watch(sessionProvider).value?.userName ?? '';
    // Recommendations are prepared in the background on first use; look again shortly.
    final preparing = home.value != null && home.value!.forYou == null;
    if (preparing && _waiting == null) {
      _waiting = Timer.periodic(const Duration(seconds: 15), (_) => ref.invalidate(homeProvider));
    } else if (!preparing && home.value != null) {
      _waiting?.cancel();
      _waiting = null;
    }
    return Scaffold(
      body: RefreshIndicator(
        color: accent,
        backgroundColor: surface,
        onRefresh: () async => ref.invalidate(homeProvider),
        child: CustomScrollView(slivers: [
          SliverToBoxAdapter(
              child: SafeArea(bottom: false, child: _Header(greeting: _greeting(), name: name, unread: home.value?.unreadNotifications ?? 0))),
          const SliverToBoxAdapter(child: UpdateBanner()),
          ...home.when(
            loading: () => [const SliverToBoxAdapter(child: _HomeSkeleton())],
            error: (e, _) => [SliverFillRemaining(hasScrollBody: false, child: ErrorView(e, onRetry: () => ref.invalidate(homeProvider)))],
            data: (h) => _shelves(h),
          ),
          const SliverToBoxAdapter(child: SizedBox(height: 190)),
        ]),
      ),
    );
  }

  List<Widget> _shelves(Home h) {
    final player = ref.read(playerProvider);
    final f = h.forYou;
    var i = 0;
    Widget shelf(Widget child) => SliverToBoxAdapter(child: FadeSlideIn(index: i++, child: child));
    final recent = h.section('recently_played');
    final top = h.section('top');
    final added = h.section('recently_added');
    final requested = h.section('requested');
    return [
      shelf(_Collection(home: h)),
      if (h.podcasts.continueListening.isNotEmpty)
        shelf(_Shelf(
          title: 'Continue listening',
          height: 108,
          children: [for (final e in h.podcasts.continueListening) _ContinueCard(episode: e)],
        )),
      if (h.releases.isNotEmpty)
        shelf(_Shelf(
          title: 'New releases',
          subtitle: 'From the artists you follow',
          height: 236,
          children: [for (final a in h.releases) _ReleaseCard(album: a)],
        )),
      if (f == null) shelf(const _Preparing()),
      if (f != null && f.songs.isNotEmpty)
        shelf(_Shelf(
          title: 'Made for you',
          subtitle: 'Tap one to hear a preview',
          height: 248,
          children: [for (final s in f.songs) _RecSongCard(song: s)],
        )),
      if (recent != null)
        shelf(_Shelf(
          title: 'Jump back in',
          height: 206,
          children: [
            for (var k = 0; k < recent.tracks.length; k++)
              _TrackCard(track: recent.tracks[k], list: recent.tracks, onTap: () => player.play(recent.tracks, index: k)),
          ],
        )),
      if (f != null && f.artists.isNotEmpty)
        shelf(_Shelf(
          title: 'Artists you might like',
          height: 196,
          children: [for (final a in f.artists) _ArtistCard(artist: a, showReason: true)],
        )),
      if (h.podcasts.newEpisodes.isNotEmpty)
        shelf(_Shelf(
          title: 'New from your podcasts',
          height: 230,
          children: [for (final e in h.podcasts.newEpisodes) _EpisodeCard(episode: e)],
        )),
      if (f != null && f.podcasts.isNotEmpty)
        shelf(_Shelf(
          title: 'Podcasts for you',
          height: 226,
          children: [for (final p in f.podcasts) PodcastCard(podcast: p, showReason: true)],
        )),
      if (top != null)
        shelf(_Shelf(
          title: 'On repeat',
          subtitle: 'Your most played this month',
          height: 206,
          children: [
            for (var k = 0; k < top.tracks.length; k++) _TrackCard(track: top.tracks[k], list: top.tracks, onTap: () => player.play(top.tracks, index: k)),
          ],
        )),
      if (f != null && f.topArtists.isNotEmpty)
        shelf(_Shelf(
          title: 'Your favourite artists',
          height: 170,
          children: [for (final a in f.topArtists) _ArtistCard(artist: a, size: 104)],
        )),
      if (h.podcasts.following.isNotEmpty)
        shelf(_Shelf(
          title: 'Your podcasts',
          height: 206,
          children: [for (final p in h.podcasts.following) PodcastCard(podcast: p)],
        )),
      if (added != null)
        shelf(_Shelf(
          title: 'Fresh on your server',
          height: 206,
          children: [
            for (var k = 0; k < added.tracks.length; k++) _TrackCard(track: added.tracks[k], list: added.tracks, onTap: () => player.play(added.tracks, index: k)),
          ],
        )),
      if (requested != null)
        shelf(_Shelf(
          title: 'Your requests',
          height: 206,
          children: [
            for (var k = 0; k < requested.tracks.length; k++)
              _TrackCard(track: requested.tracks[k], list: requested.tracks, onTap: () => player.play(requested.tracks, index: k)),
          ],
        )),
    ];
  }
}

/// "A new version is ready": shown once the server's newer build has downloaded.
class UpdateBanner extends ConsumerWidget {
  const UpdateBanner({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final u = ref.watch(updaterProvider);
    final update = u.update;
    return AnimatedSize(
      duration: const Duration(milliseconds: 350),
      curve: Curves.easeOutCubic,
      child: update == null
          ? const SizedBox(width: double.infinity)
          : Padding(
              padding: const EdgeInsets.fromLTRB(16, 14, 16, 0),
              child: FadeSlideIn(
                child: Glass(
                  blur: false,
                  radius: radiusL,
                  padding: const EdgeInsets.fromLTRB(16, 14, 12, 14),
                  child: Row(children: [
                    Container(
                      width: 44,
                      height: 44,
                      decoration: const BoxDecoration(shape: BoxShape.circle, gradient: aurora),
                      child: const Icon(Icons.system_update_rounded, size: 24),
                    ),
                    const SizedBox(width: 14),
                    Expanded(
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        Text('${ref.watch(brandProvider).name} ${update.version} is ${u.ready ? 'ready' : 'downloading'}',
                            style: const TextStyle(fontWeight: FontWeight.w800)),
                        Text(update.notes.isEmpty ? 'A new version from your server.' : update.notes,
                            maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 12.5)),
                      ]),
                    ),
                    const SizedBox(width: 10),
                    if (!u.ready)
                      SizedBox(
                        width: 28,
                        height: 28,
                        child: CircularProgressIndicator(strokeWidth: 2.5, value: (u.progress ?? 0) > 0 ? u.progress : null),
                      )
                    else
                      Pressable(
                        onTap: () async {
                          final msg = await ref.read(updaterProvider.notifier).install();
                          if (msg != null && context.mounted) toast(context, msg);
                        },
                        child: Container(
                          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                          decoration: BoxDecoration(
                            borderRadius: BorderRadius.circular(20),
                            gradient: aurora,
                            boxShadow: [BoxShadow(color: accent2.withValues(alpha: 0.4), blurRadius: 14)],
                          ),
                          child: const Text('Install', style: TextStyle(fontWeight: FontWeight.w800)),
                        ),
                      ),
                  ]),
                ),
              ),
            ),
    );
  }
}

class _Header extends StatelessWidget {
  const _Header({required this.greeting, required this.name, this.unread = 0});
  final String greeting;
  final String name;
  final int unread;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(20, 16, 16, 4),
        child: Row(crossAxisAlignment: CrossAxisAlignment.end, children: [
          Expanded(
            child: FadeSlideIn(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(greeting, style: const TextStyle(color: muted, fontSize: 16, fontWeight: FontWeight.w500)),
                if (name.isNotEmpty) GradientMask(child: Text(name, style: Theme.of(context).textTheme.headlineLarge)),
              ]),
            ),
          ),
          Stack(clipBehavior: Clip.none, children: [
            GlassIconButton(
              icon: unread > 0 ? Icons.notifications_active_rounded : Icons.notifications_none_rounded,
              tooltip: 'Notifications',
              onPressed: () => push(context, const NotificationsScreen()),
            ),
            if (unread > 0)
              Positioned(
                right: -2,
                top: -2,
                child: IgnorePointer(
                  child: Container(
                    constraints: const BoxConstraints(minWidth: 20),
                    padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 2),
                    decoration: BoxDecoration(gradient: aurora, borderRadius: BorderRadius.circular(10)),
                    child: Text(unread > 99 ? '99+' : '$unread', textAlign: TextAlign.center,
                        style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w800)),
                  ),
                ),
              ),
          ]),
          const SizedBox(width: 10),
          GlassIconButton(icon: Icons.settings_rounded, tooltip: 'Settings', onPressed: () => push(context, const SettingsScreen())),
        ]),
      );
}

/// A new release from an artist you follow: opens the album, ready to add.
class _ReleaseCard extends StatelessWidget {
  const _ReleaseCard({required this.album});
  final CatalogAlbum album;

  @override
  Widget build(BuildContext context) => Pressable(
        onTap: () => push(context, CatalogAlbumScreen(album)),
        child: SizedBox(
          width: 150,
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Stack(children: [
              Cover(album.thumbUrl, size: 150, radius: radiusM, icon: Icons.album_rounded),
              Positioned(
                left: 8,
                top: 8,
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                  decoration: BoxDecoration(color: Colors.black.withValues(alpha: 0.6), borderRadius: BorderRadius.circular(10)),
                  child: Text(album.typeLabel, style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w700)),
                ),
              ),
            ]),
            const SizedBox(height: 8),
            Text(album.name, maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13.5)),
            const SizedBox(height: 2),
            Text(album.artistLine, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 12)),
            Text(releaseAge(album.releaseDate), maxLines: 1, style: const TextStyle(color: accent2, fontSize: 12, fontWeight: FontWeight.w600)),
          ]),
        ),
      );
}

/// A titled, horizontally scrolling row of cards.
class _Shelf extends StatelessWidget {
  const _Shelf({required this.title, this.subtitle, required this.height, required this.children});
  final String title;
  final String? subtitle;
  final double height;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) => Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        SectionTitle(title, subtitle: subtitle),
        SizedBox(
          height: height,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 20),
            itemCount: children.length,
            separatorBuilder: (_, _) => const SizedBox(width: 14),
            itemBuilder: (context, i) => FadeSlideIn(index: i, axis: Axis.horizontal, offset: 24, child: children[i]),
          ),
        ),
      ]);
}

/// Liked Songs and your playlists, as a row of tall cards. Press and hold a playlist to drag it
/// somewhere else: the order is saved for all your devices (and the Library follows it).
class _Collection extends ConsumerStatefulWidget {
  const _Collection({required this.home});
  final Home home;

  @override
  ConsumerState<_Collection> createState() => _CollectionState();
}

class _CollectionState extends ConsumerState<_Collection> {
  late List<PlaylistSummary> _items = _fromHome();

  List<PlaylistSummary> _fromHome() => [for (final p in widget.home.playlists) if (p.count > 0) p];

  @override
  void didUpdateWidget(_Collection old) {
    super.didUpdateWidget(old);
    if (old.home != widget.home) _items = _fromHome();
  }

  Future<void> _moved(int from, int to) async {
    setState(() => _items.insert(to, _items.removeAt(from)));
    final api = ref.read(apiProvider);
    if (api == null) return;
    try {
      // Home shows some of your playlists: put these in their new order in the slots they
      // take in your whole list, so the ones not shown here stay where they are.
      final full = [for (final p in (await api.library()).playlists) p.id];
      final shown = [for (final p in _items) if (full.contains(p.id)) p.id];
      final slots = [for (var i = 0; i < full.length; i++) if (shown.contains(full[i])) i];
      for (var k = 0; k < slots.length; k++) {
        full[slots[k]] = shown[k];
      }
      await api.setPlaylistOrder(full);
      ref.invalidate(libraryProvider);
    } on ApiException catch (e) {
      if (mounted) toast(context, 'Couldn\'t save the order: ${e.message}');
    }
  }

  @override
  Widget build(BuildContext context) => SizedBox(
        height: 196,
        child: ReorderableListView.builder(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.fromLTRB(20, 16, 8, 0),
          header: Padding(padding: const EdgeInsets.only(right: 12), child: _LikedCard(count: widget.home.likedCount)),
          itemCount: _items.length,
          onReorderStart: (_) => HapticFeedback.mediumImpact(),
          onReorderItem: _moved,
          proxyDecorator: (child, _, animation) => AnimatedBuilder(
            animation: animation,
            builder: (context, child) {
              final t = Curves.easeOutCubic.transform(animation.value);
              return Transform.scale(
                scale: 1 + 0.06 * t,
                child: DecoratedBox(
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(radiusL),
                    boxShadow: [BoxShadow(color: accent2.withValues(alpha: 0.45 * t), blurRadius: 26)],
                  ),
                  child: Material(type: MaterialType.transparency, child: child),
                ),
              );
            },
            child: child,
          ),
          itemBuilder: (context, i) {
            final p = _items[i];
            return Padding(
              key: ValueKey(p.id),
              padding: const EdgeInsets.only(right: 12),
              child: _PlaylistCard(playlist: p),
            );
          },
        ),
      );
}

class _LikedCard extends StatelessWidget {
  const _LikedCard({required this.count});
  final int count;

  @override
  Widget build(BuildContext context) => Pressable(
        onTap: () => push(context, const TrackListScreen.liked()),
        child: Glass(
          blur: false,
          radius: radiusL,
          child: SizedBox(
            width: 150,
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                const Expanded(child: Center(child: LikedCover(size: 92, radius: 46))),
                const Text('Liked Songs', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 15)),
                Text('$count songs', style: const TextStyle(color: muted, fontSize: 12.5)),
              ]),
            ),
          ),
        ),
      );
}

class _PlaylistCard extends StatelessWidget {
  const _PlaylistCard({required this.playlist});
  final PlaylistSummary playlist;

  @override
  Widget build(BuildContext context) {
    final p = playlist;
    return Pressable(
      onTap: () => push(context, TrackListScreen.playlist(p.id, p.name)),
      child: SizedBox(
        width: 150,
        child: ClipRRect(
          borderRadius: BorderRadius.circular(radiusL),
          child: Stack(fit: StackFit.expand, children: [
            Cover(p.imageUrl, size: 180, radius: 0, icon: Icons.queue_music_rounded),
            const DecoratedBox(
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topCenter,
                  end: Alignment.bottomCenter,
                  colors: [Colors.transparent, Color(0xDD07060F)],
                  stops: [0.4, 1],
                ),
              ),
            ),
            Positioned(
              left: 12,
              right: 12,
              bottom: 12,
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(p.name, maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 14)),
                Text('${p.count} songs', style: const TextStyle(color: Colors.white70, fontSize: 12)),
              ]),
            ),
          ]),
        ),
      ),
    );
  }
}

class _RecSongCard extends ConsumerWidget {
  const _RecSongCard({required this.song});
  final RecSong song;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = song.track;
    final badge = song.playable
        ? null
        : t != null
            ? Icons.hourglass_top_rounded
            : Icons.add_rounded;
    return Pressable(
      onTap: () => showPreview(context, ref, item: song.catalogItem, track: t, reason: song.reason),
      child: SizedBox(
        width: 156,
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Stack(children: [
            Container(
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(radiusM),
                boxShadow: [BoxShadow(color: accent.withValues(alpha: 0.18), blurRadius: 18, offset: const Offset(0, 8))],
              ),
              child: Cover(song.coverUrl, size: 156, radius: radiusM),
            ),
            Positioned(
              right: 8,
              bottom: 8,
              child: Glass(
                radius: 16,
                child: SizedBox(
                  width: 32,
                  height: 32,
                  child: Icon(badge ?? Icons.play_arrow_rounded, size: 20, color: Colors.white),
                ),
              ),
            ),
          ]),
          const SizedBox(height: 8),
          Text(song.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700)),
          Text(song.artistLine, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 12.5)),
          if (song.reason != null)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Row(children: [
                const Icon(Icons.auto_awesome_rounded, size: 11, color: glowColor),
                const SizedBox(width: 4),
                Expanded(
                  child: Text(song.reason!, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: glowColor, fontSize: 11)),
                ),
              ]),
            ),
        ]),
      ),
    );
  }
}

class _TrackCard extends ConsumerWidget {
  const _TrackCard({required this.track, required this.onTap, this.list});
  final Track track;
  final VoidCallback onTap;
  final List<Track>? list;

  @override
  Widget build(BuildContext context, WidgetRef ref) => Pressable(
        onTap: onTap,
        onLongPress: () => showTrackMenu(context, ref, track, list: list),
        child: SizedBox(
          width: 140,
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Opacity(opacity: track.playable ? 1 : 0.5, child: Cover(track.coverUrl, size: 140, radius: radiusM)),
            const SizedBox(height: 8),
            Text(track.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700)),
            Text(track.playable ? track.artistLine : track.statusText,
                maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 12.5)),
          ]),
        ),
      );
}

class _ArtistCard extends StatelessWidget {
  const _ArtistCard({required this.artist, this.size = 120, this.showReason = false});
  final ArtistRec artist;
  final double size;
  final bool showReason;

  @override
  Widget build(BuildContext context) => Pressable(
        onTap: () => push(context, ArtistScreen(artist.name)),
        child: SizedBox(
          width: size + 8,
          child: Column(children: [
            ArtistAvatar(artist.thumbUrl, size: size),
            const SizedBox(height: 8),
            Text(artist.name, maxLines: 1, overflow: TextOverflow.ellipsis, textAlign: TextAlign.center,
                style: const TextStyle(fontWeight: FontWeight.w700)),
            if (showReason && artist.reason != null)
              Text(artist.reason!.replaceFirst('Because you like ', 'Like '),
                  maxLines: 1, overflow: TextOverflow.ellipsis, textAlign: TextAlign.center, style: const TextStyle(color: muted, fontSize: 11.5)),
          ]),
        ),
      );
}

class _ContinueCard extends ConsumerWidget {
  const _ContinueCard({required this.episode});
  final Episode episode;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final left = episode.duration - episode.progress;
    return Pressable(
      onTap: () => ref.read(playerProvider).play([episode.toTrack()]),
      child: Glass(
        blur: false,
        radius: radiusM,
        child: SizedBox(
          width: 290,
          child: Padding(
            padding: const EdgeInsets.all(10),
            child: Row(children: [
              Cover(episode.imageUrl, size: 76, radius: radiusS, icon: Icons.podcasts_rounded),
              const SizedBox(width: 12),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisAlignment: MainAxisAlignment.center, children: [
                  Text(episode.title, maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13.5)),
                  const SizedBox(height: 2),
                  Text(episode.podcastTitle, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 12)),
                  const SizedBox(height: 8),
                  Row(children: [
                    Expanded(child: _ThinProgress(episode.fraction)),
                    const SizedBox(width: 8),
                    Text(episode.duration == Duration.zero ? '' : '${formatLength(left)} left', style: const TextStyle(color: muted, fontSize: 11)),
                  ]),
                ]),
              ),
            ]),
          ),
        ),
      ),
    );
  }
}

class _ThinProgress extends StatelessWidget {
  const _ThinProgress(this.value);
  final double value;

  @override
  Widget build(BuildContext context) => ClipRRect(
        borderRadius: BorderRadius.circular(2),
        child: SizedBox(
          height: 3,
          child: Stack(children: [
            Container(color: Colors.white12),
            FractionallySizedBox(
              widthFactor: value.clamp(0.0, 1.0),
              child: Container(decoration: const BoxDecoration(gradient: auroraSoft)),
            ),
          ]),
        ),
      );
}

class _EpisodeCard extends ConsumerWidget {
  const _EpisodeCard({required this.episode});
  final Episode episode;

  @override
  Widget build(BuildContext context, WidgetRef ref) => Pressable(
        onTap: () => ref.read(playerProvider).play([episode.toTrack()]),
        onLongPress: () => push(context, PodcastScreen(episode.podcastId, title: episode.podcastTitle)),
        child: SizedBox(
          width: 150,
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Stack(children: [
              Cover(episode.imageUrl, size: 150, radius: radiusM, icon: Icons.podcasts_rounded),
              const Positioned(
                right: 8,
                bottom: 8,
                child: Glass(radius: 16, child: SizedBox(width: 32, height: 32, child: Icon(Icons.play_arrow_rounded, size: 20))),
              ),
            ]),
            const SizedBox(height: 8),
            Text(episode.title, maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13.5)),
            Text('${formatDate(episode.published)} · ${episode.podcastTitle}',
                maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 12)),
          ]),
        ),
      );
}

class _Preparing extends StatelessWidget {
  const _Preparing();

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(20, 24, 20, 0),
        child: Glass(
          blur: false,
          child: SizedBox(
            height: 130,
            child: Stack(children: [
              const Positioned.fill(child: AmbientSparkles(perSecond: 5)),
              Padding(
                padding: const EdgeInsets.all(20),
                child: Row(children: [
                  const GradientMask(child: Icon(Icons.auto_awesome_rounded, size: 40)),
                  const SizedBox(width: 16),
                  Expanded(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisAlignment: MainAxisAlignment.center, children: [
                      Text('Finding music for you…', style: Theme.of(context).textTheme.titleMedium),
                      const SizedBox(height: 4),
                      const Text('Your server is picking songs, artists and podcasts from what you love. This takes a moment the first time.',
                          style: TextStyle(color: muted, fontSize: 13)),
                    ]),
                  ),
                ]),
              ),
            ]),
          ),
        ),
      );
}

class _HomeSkeleton extends StatelessWidget {
  const _HomeSkeleton();

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(20, 16, 0, 0),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          for (var row = 0; row < 3; row++) ...[
            if (row > 0) const Padding(padding: EdgeInsets.fromLTRB(0, 28, 0, 14), child: ShimmerBox(width: 180, height: 20, radius: 8)),
            SizedBox(
              height: row == 0 ? 180 : 190,
              child: ListView(
                scrollDirection: Axis.horizontal,
                physics: const NeverScrollableScrollPhysics(),
                children: [
                  for (var k = 0; k < 4; k++)
                    const Padding(padding: EdgeInsets.only(right: 14), child: ShimmerBox(width: 150, height: 150, radius: radiusL)),
                ],
              ),
            ),
          ],
        ]),
      );
}
