import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/models.dart';
import '../state/data.dart';
import '../state/offline.dart';
import '../state/player.dart';
import '../state/session.dart';
import 'arrange_playlists_screen.dart';
import 'download_progress.dart';
import 'fx/clock.dart';
import 'fx/motion.dart';
import 'podcast_screen.dart';
import 'settings_screen.dart';
import 'theme.dart';
import 'tracklist_screen.dart';
import 'widgets.dart';

enum _Filter { all, playlists, artists, podcasts }

class LibraryScreen extends ConsumerStatefulWidget {
  const LibraryScreen({super.key});
  @override
  ConsumerState<LibraryScreen> createState() => _LibraryScreenState();
}

class _LibraryScreenState extends ConsumerState<LibraryScreen> {
  _Filter _filter = _Filter.all;
  Timer? _downloads;

  @override
  void initState() {
    super.initState();
    // Songs you added, on their way: Requested songs keeps up while it shows (not unseen, in the background).
    _downloads = Timer.periodic(const Duration(seconds: 6), (_) {
      if (mounted && showing(context) && ref.read(requestProgressProvider).value != null) ref.invalidate(requestProgressProvider);
    });
  }

  @override
  void dispose() {
    _downloads?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final lib = ref.watch(libraryProvider);
    final offline = ref.watch(offlineProvider);
    final downloading = _filter == _Filter.all || _filter == _Filter.playlists ? ref.watch(requestProgressProvider).value : null;
    final shows = _filter == _Filter.playlists ? null : ref.watch(podcastsProvider);
    final artists = _filter == _Filter.all || _filter == _Filter.artists ? ref.watch(followingProvider) : null;
    var n = 0;
    Widget row(Widget w) => FadeSlideIn(index: n++, child: w);
    return Scaffold(
      body: RefreshIndicator(
        color: accent,
        backgroundColor: surface,
        onRefresh: () async {
          ref.invalidate(libraryProvider);
          ref.invalidate(podcastsProvider);
          ref.invalidate(followingProvider);
        },
        child: ListView(padding: const EdgeInsets.only(bottom: 190), children: [
          SafeArea(
            bottom: false,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(20, 16, 16, 0),
              child: Row(children: [
                Expanded(child: Text('Your Library', style: Theme.of(context).textTheme.headlineLarge)),
                GlassIconButton(
                  icon: Icons.swap_vert_rounded,
                  tooltip: 'Arrange playlists',
                  onPressed: lib.value == null || lib.value!.playlists.isEmpty ? null : () => _arrange(context, lib.value!.playlists),
                ),
                const SizedBox(width: 10),
                GlassIconButton(icon: Icons.add_rounded, tooltip: 'New playlist', onPressed: () => _create(context)),
                const SizedBox(width: 10),
                GlassIconButton(icon: Icons.settings_rounded, tooltip: 'Settings', onPressed: () => push(context, const SettingsScreen())),
              ]),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 14, 16, 6),
            child: Row(children: [
              for (final f in _Filter.values)
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: FilterPill(
                    label: switch (f) {
                      _Filter.all => 'All',
                      _Filter.playlists => 'Playlists',
                      _Filter.artists => 'Artists',
                      _Filter.podcasts => 'Podcasts',
                    },
                    selected: _filter == f,
                    onTap: () => setState(() => _filter = f),
                  ),
                ),
            ]),
          ),
          if (_filter == _Filter.artists)
            ...?artists?.when(
              loading: () => [const ListSkeleton(rows: 4)],
              error: (e, _) => [ErrorView(e, onRetry: () => ref.invalidate(followingProvider))],
              data: (list) => list.isEmpty
                  ? [
                      const Padding(
                        padding: EdgeInsets.fromLTRB(32, 40, 32, 0),
                        child: Text('Follow artists to hear about their new releases: open an artist and tap Follow.',
                            textAlign: TextAlign.center, style: TextStyle(color: muted, height: 1.4)),
                      ),
                    ]
                  : [
                      for (final a in list)
                        row(_LibraryRow(
                          leading: ArtistAvatar(a.imageUrl, size: 60, ring: false),
                          title: a.name,
                          subtitle: a.latestName == null ? 'Artist' : 'Latest: ${a.latestName} · ${releaseAge(a.latestDate)}',
                          onTap: () => push(context, ArtistScreen(a.name)),
                        )),
                    ],
            ),
          if (_filter != _Filter.podcasts && _filter != _Filter.artists) ...[
            row(_LibraryRow(
              leading: const LikedCover(size: 60),
              title: 'Liked Songs',
              subtitle: lib.whenOrNull(data: (l) => '${l.likedCount} songs') ?? '',
              onTap: () => push(context, const TrackListScreen.liked()),
            )),
            row(_LibraryRow(
              leading: _IconCover(Icons.download_done_rounded),
              title: 'Downloads',
              subtitle: '${offline.downloads.length} songs on this device · ${formatBytes(offline.downloadBytes)}',
              onTap: () => push(context, const TrackListScreen.downloads()),
            )),
            row(_LibraryRow(
              leading: _IconCover(downloading == null ? Icons.library_add_rounded : Icons.downloading_rounded),
              title: 'Requested songs',
              subtitle: downloading?.headline ?? 'Songs you added from search, charts and previews',
              progress: downloading?.fraction,
              onTap: () => push(context, const RequestsScreen()),
            )),
          ],
          if (_filter == _Filter.all)
            row(_LibraryRow(
              leading: _IconCover(Icons.person_rounded),
              title: 'Artists you follow',
              subtitle: artists?.whenOrNull(data: (a) => a.isEmpty ? 'Follow artists to hear about their new releases' : '${a.length} artist${a.length == 1 ? '' : 's'}') ?? '',
              onTap: () => setState(() => _filter = _Filter.artists),
            )),
          if (_filter == _Filter.all || _filter == _Filter.podcasts)
            row(_LibraryRow(
              leading: _IconCover(Icons.podcasts_rounded),
              title: 'Podcasts',
              subtitle: shows?.whenOrNull(data: (s) => s.following.isEmpty ? 'Find shows to follow' : '${s.following.length} shows you follow') ?? '',
              onTap: () => push(context, const PodcastsScreen()),
            )),
          if (_filter == _Filter.podcasts)
            ...?shows?.whenOrNull(
              data: (s) => [
                for (final p in s.following)
                  row(_LibraryRow(
                    leading: Cover(p.artworkUrl, size: 60, icon: Icons.podcasts_rounded),
                    title: p.title,
                    subtitle: 'Podcast · ${p.author ?? ''}',
                    onTap: () => push(context, PodcastScreen(p.id, title: p.title)),
                  )),
              ],
            ),
          if (_filter == _Filter.all || _filter == _Filter.playlists)
            ...lib.when(
              loading: () => [const ListSkeleton(rows: 5)],
              error: (e, _) => [ErrorView(e, onRetry: () => ref.invalidate(libraryProvider))],
              data: (l) => [
                for (final p in l.playlists)
                  row(_LibraryRow(
                    leading: Cover(p.imageUrl, size: 60, icon: Icons.queue_music_rounded),
                    title: p.name,
                    subtitle: 'Playlist · ${p.origin} · ${p.count} songs',
                    onTap: () => push(context, TrackListScreen.playlist(p.id, p.name)),
                    onLongPress: () => _arrange(context, l.playlists, focus: p.id),
                  )),
              ],
            ),
        ]),
      ),
    );
  }

  void _arrange(BuildContext context, List<PlaylistSummary> playlists, {String? focus}) {
    HapticFeedback.mediumImpact();
    push(context, ArrangePlaylistsScreen(playlists: playlists, focus: focus));
  }

  Future<void> _create(BuildContext context) async {
    final name = await askText(context, 'New playlist', 'Name');
    if (name == null || name.trim().isEmpty) return;
    final page = await ref.read(apiProvider)?.createPlaylist(name.trim());
    ref.invalidate(libraryProvider);
    if (page != null && context.mounted) push(context, TrackListScreen.playlist(page.info['id'] as String, page.name));
  }
}

class _IconCover extends StatelessWidget {
  const _IconCover(this.icon);
  final IconData icon;

  @override
  Widget build(BuildContext context) => Container(
        width: 60,
        height: 60,
        decoration: BoxDecoration(borderRadius: BorderRadius.circular(radiusS), color: glassFill, border: Border.all(color: glassBorder)),
        child: GradientMask(child: Icon(icon, size: 28)),
      );
}

class _LibraryRow extends StatelessWidget {
  const _LibraryRow({required this.leading, required this.title, required this.subtitle, required this.onTap, this.onLongPress, this.progress});
  final Widget leading;
  final String title;
  final String subtitle;
  final VoidCallback onTap;
  final VoidCallback? onLongPress;
  final double? progress; // a bar under the subtitle (songs downloading)

  @override
  Widget build(BuildContext context) => Pressable(
        scale: 0.98,
        onTap: onTap,
        onLongPress: onLongPress,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 7),
          child: Row(children: [
            leading,
            const SizedBox(width: 14),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 15.5)),
                const SizedBox(height: 2),
                Text(subtitle, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 13)),
                if (progress != null) Padding(padding: const EdgeInsets.only(top: 7, right: 8), child: DownloadBar(value: progress!)),
              ]),
            ),
          ]),
        ),
      );
}

/// Songs requested from the app, and how their downloads are getting on: a bar for all of them at the
/// top ("12 of 547 songs ready"), and each song the server is working on with its stage and its own
/// bar. Looks again every few seconds while songs download and the screen shows.
class RequestsScreen extends ConsumerStatefulWidget {
  const RequestsScreen({super.key});
  @override
  ConsumerState<RequestsScreen> createState() => _RequestsScreenState();
}

class _RequestsScreenState extends ConsumerState<RequestsScreen> {
  Timer? _timer;
  int _ticks = 0;

  @override
  void initState() {
    super.initState();
    // As songs arrive the list catches up (they turn playable), and after the last one so does the rest of the app.
    ref.listenManual<AsyncValue<DownloadProgress?>>(requestProgressProvider, (before, now) {
      final was = before?.value;
      if (now.isLoading || now.hasError || was == null) return;
      final p = now.value;
      if (p == null) {
        ref.refreshLibrary();
      } else if (p.ready != was.ready || p.total != was.total) {
        ref.invalidate(requestsProvider);
      }
    });
    _timer = Timer.periodic(const Duration(seconds: 3), (_) {
      if (!mounted || !showing(context)) return; // (not unseen, in the background)
      _ticks++;
      final p = ref.read(requestProgressProvider).value;
      if (p != null && (p.active.isNotEmpty || _ticks % 5 == 0)) ref.invalidate(requestProgressProvider);
      if (_ticks % 7 == 0) {
        ref.invalidate(requestsProvider);
        if (p == null) ref.invalidate(requestProgressProvider); // (added from another phone)
      }
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final value = ref.watch(requestsProvider);
    final progress = ref.watch(requestProgressProvider).value;
    final player = ref.read(playerProvider);
    return Scaffold(
      appBar: AppBar(
        leading: Padding(padding: const EdgeInsets.all(6), child: GlassIconButton(icon: Icons.arrow_back_rounded, onPressed: () => Navigator.maybePop(context))),
        title: const Text('Requested songs'),
      ),
      body: RefreshIndicator(
        color: accent,
        backgroundColor: surface,
        onRefresh: () async {
          ref.invalidate(requestsProvider);
          ref.invalidate(requestProgressProvider);
        },
        child: asyncView<List<Track>>(value, (tracks) {
          if (tracks.isEmpty) {
            return ListView(children: const [
              Padding(
                padding: EdgeInsets.all(32),
                child: Text('Find songs that aren\'t in your library under Search, or like a preview, and they\'ll be listed here while they download.',
                    textAlign: TextAlign.center, style: TextStyle(color: muted)),
              ),
            ]);
          }
          final head = progress == null ? 0 : 1;
          return ListView.builder(
            padding: const EdgeInsets.only(bottom: 190),
            itemCount: head + tracks.length,
            itemBuilder: (context, i) {
              if (i < head) return DownloadProgressCard(progress!);
              final n = i - head;
              final now = progress?.of(tracks[n].id);
              return FadeSlideIn(
                index: i,
                child: TrackTile(
                  track: tracks[n],
                  list: tracks,
                  note: now?.stageText,
                  footer: now == null ? null : DownloadBar(value: now.fraction),
                  onTap: () => player.play(tracks, index: n),
                ),
              );
            },
          );
        }, onRetry: () => ref.invalidate(requestsProvider), loading: const ListSkeleton()),
      ),
    );
  }
}
