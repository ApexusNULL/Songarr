import 'dart:async';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../state/data.dart';
import '../state/player.dart';
import '../state/search_history.dart';
import '../state/session.dart';
import 'artist_about.dart';
import 'catalog_album_screen.dart';
import 'fx/motion.dart';
import 'podcast_screen.dart';
import 'preview_sheet.dart';
import 'theme.dart';
import 'tracklist_screen.dart';
import 'widgets.dart';

// Each genre gets its own two-colour glow.
const _genreGradients = [
  [Color(0xFF8B5CF6), Color(0xFFEC4899)],
  [Color(0xFFF97316), Color(0xFFDB2777)],
  [Color(0xFF2563EB), Color(0xFF7C3AED)],
  [Color(0xFF059669), Color(0xFF0EA5E9)],
  [Color(0xFFE11D48), Color(0xFFF59E0B)],
  [Color(0xFF16A34A), Color(0xFF84CC16)],
  [Color(0xFFB45309), Color(0xFFEAB308)],
  [Color(0xFF6D28D9), Color(0xFF312E81)],
  [Color(0xFF0284C7), Color(0xFF22D3EE)],
  [Color(0xFFA21CAF), Color(0xFF6366F1)],
  [Color(0xFFDC2626), Color(0xFF7F1D1D)],
  [Color(0xFF0F766E), Color(0xFF4F46E5)],
];

class SearchScreen extends ConsumerStatefulWidget {
  const SearchScreen({super.key});
  @override
  ConsumerState<SearchScreen> createState() => _SearchScreenState();
}

class _SearchScreenState extends ConsumerState<SearchScreen> {
  final _controller = TextEditingController();
  final _focus = FocusNode();
  Timer? _debounce;
  String _query = '';

  @override
  void initState() {
    super.initState();
    _focus.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _controller.dispose();
    _focus.dispose();
    super.dispose();
  }

  void _onChanged(String v) {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 350), () => setState(() => _query = v.trim()));
  }

  void _remember() => ref.read(searchHistoryProvider.notifier).add(_query);

  /// Search again for something from the history.
  void _searchFor(String q) {
    _debounce?.cancel();
    _controller.text = q;
    _controller.selection = TextSelection.collapsed(offset: q.length);
    setState(() => _query = q);
    ref.read(searchHistoryProvider.notifier).add(q); // back to the top
    _focus.unfocus();
  }

  // A tap anywhere in the results (not a scroll) means this search found something: remember it.
  Offset? _down;
  DateTime _downAt = DateTime.fromMillisecondsSinceEpoch(0);

  @override
  Widget build(BuildContext context) {
    final focused = _focus.hasFocus;
    return Scaffold(
      body: SafeArea(
        bottom: false,
        child: Column(children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 16, 20, 4),
            child: Align(alignment: Alignment.centerLeft, child: Text('Search', style: Theme.of(context).textTheme.headlineLarge)),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 10, 16, 8),
            child: AnimatedContainer(
              duration: const Duration(milliseconds: 300),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(radiusL),
                boxShadow: focused ? [BoxShadow(color: accent.withValues(alpha: 0.35), blurRadius: 24)] : const [],
              ),
              child: Glass(
                radius: radiusL,
                child: TextField(
                  controller: _controller,
                  focusNode: _focus,
                  onChanged: _onChanged,
                  textInputAction: TextInputAction.search,
                  onSubmitted: (v) {
                    _debounce?.cancel();
                    setState(() => _query = v.trim());
                    _remember();
                  },
                  style: const TextStyle(fontSize: 16),
                  decoration: InputDecoration(
                    hintText: 'Songs, artists, albums, podcasts',
                    hintStyle: const TextStyle(color: muted),
                    prefixIcon: focused ? const GradientMask(child: Icon(Icons.search_rounded)) : const Icon(Icons.search_rounded, color: muted),
                    suffixIcon: _query.isEmpty
                        ? null
                        : IconButton(
                            icon: const Icon(Icons.close_rounded),
                            onPressed: () {
                              _controller.clear();
                              setState(() => _query = '');
                            }),
                    filled: false,
                    border: InputBorder.none,
                    enabledBorder: InputBorder.none,
                    focusedBorder: InputBorder.none,
                    contentPadding: const EdgeInsets.symmetric(vertical: 16),
                  ),
                ),
              ),
            ),
          ),
          Expanded(
            child: AnimatedSwitcher(
              duration: const Duration(milliseconds: 300),
              child: _query.isEmpty
                  ? _Browse(key: const ValueKey('browse'), onSearch: _searchFor)
                  : Listener(
                      key: ValueKey(_query),
                      onPointerDown: (e) {
                        _down = e.position;
                        _downAt = DateTime.now();
                      },
                      onPointerUp: (e) {
                        final d = _down;
                        if (d != null && (e.position - d).distance < 16 && DateTime.now().difference(_downAt) < const Duration(milliseconds: 700)) {
                          _remember();
                        }
                      },
                      child: _Results(query: _query),
                    ),
            ),
          ),
        ]),
      ),
    );
  }
}

class _Browse extends ConsumerWidget {
  const _Browse({super.key, required this.onSearch});
  final void Function(String query) onSearch;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final genres = ref.watch(genresProvider);
    final history = ref.watch(searchHistoryProvider);
    return CustomScrollView(slivers: [
      if (history.isNotEmpty) ...[
        SliverToBoxAdapter(
          child: SectionTitle('Recent searches',
              action: 'Clear', onAction: () => ref.read(searchHistoryProvider.notifier).clear(), padding: const EdgeInsets.fromLTRB(20, 6, 8, 4)),
        ),
        SliverList.builder(
          itemCount: history.length > 8 ? 8 : history.length,
          itemBuilder: (context, i) => FadeSlideIn(
            index: i,
            child: InkWell(
              onTap: () => onSearch(history[i]),
              child: Padding(
                padding: const EdgeInsets.fromLTRB(20, 2, 8, 2),
                child: Row(children: [
                  const Icon(Icons.history_rounded, color: muted, size: 22),
                  const SizedBox(width: 14),
                  Expanded(child: Text(history[i], maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 15.5))),
                  IconButton(
                    tooltip: 'Remove from history',
                    icon: const Icon(Icons.close_rounded, color: muted, size: 20),
                    onPressed: () => ref.read(searchHistoryProvider.notifier).remove(history[i]),
                  ),
                ]),
              ),
            ),
          ),
        ),
        const SliverToBoxAdapter(child: SizedBox(height: 8)),
      ],
      SliverToBoxAdapter(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
          child: Pressable(
            onTap: () => push(context, const PodcastsScreen()),
            child: Glass(
              blur: false,
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Row(children: [
                  Container(
                    width: 52,
                    height: 52,
                    decoration: const BoxDecoration(shape: BoxShape.circle, gradient: aurora),
                    child: const Icon(Icons.podcasts_rounded, size: 28),
                  ),
                  const SizedBox(width: 14),
                  const Expanded(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Text('Podcasts', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17)),
                      Text('Your shows, new episodes and what\'s popular', style: TextStyle(color: muted, fontSize: 13)),
                    ]),
                  ),
                  const Icon(Icons.chevron_right_rounded, color: muted),
                ]),
              ),
            ),
          ),
        ),
      ),
      const SliverToBoxAdapter(child: SectionTitle('Popular by genre', subtitle: 'What everyone\'s playing right now')),
      ...genres.when(
        loading: () => [
          SliverPadding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            sliver: SliverGrid.count(
              crossAxisCount: 2,
              childAspectRatio: 1.45,
              mainAxisSpacing: 12,
              crossAxisSpacing: 12,
              children: [for (var i = 0; i < 6; i++) const ShimmerBox(radius: radiusL)],
            ),
          ),
        ],
        error: (e, _) => [SliverToBoxAdapter(child: ErrorView(e, onRetry: () => ref.invalidate(genresProvider)))],
        data: (list) => [
          SliverPadding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 0),
            sliver: SliverGrid.builder(
              gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                crossAxisCount: 2,
                childAspectRatio: 1.45,
                mainAxisSpacing: 12,
                crossAxisSpacing: 12,
              ),
              itemCount: list.length,
              itemBuilder: (context, i) => FadeSlideIn(index: i, child: _GenreCard(genre: list[i], colors: _genreGradients[i % _genreGradients.length])),
            ),
          ),
        ],
      ),
      const SliverToBoxAdapter(child: SizedBox(height: 190)),
    ]);
  }
}

class _GenreCard extends StatelessWidget {
  const _GenreCard({required this.genre, required this.colors});
  final Genre genre;
  final List<Color> colors;

  @override
  Widget build(BuildContext context) => Pressable(
        onTap: () => push(context, GenreScreen(genre)),
        child: Container(
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(radiusL),
            gradient: LinearGradient(colors: colors, begin: Alignment.topLeft, end: Alignment.bottomRight),
            boxShadow: [BoxShadow(color: colors.first.withValues(alpha: 0.3), blurRadius: 18, offset: const Offset(0, 6))],
          ),
          clipBehavior: Clip.antiAlias,
          child: Stack(children: [
            if (genre.image != null)
              Positioned(
                right: -18,
                bottom: -18,
                child: Opacity(
                  opacity: 0.55,
                  child: ClipOval(
                    child: CachedNetworkImage(imageUrl: genre.image!, width: 110, height: 110, fit: BoxFit.cover, errorWidget: (_, _, _) => const SizedBox()),
                  ),
                ),
              ),
            Padding(
              padding: const EdgeInsets.all(14),
              child: Text(genre.id == 0 ? 'Top songs' : genre.name, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w800, letterSpacing: -0.3)),
            ),
          ]),
        ),
      );
}

class _Results extends ConsumerWidget {
  const _Results({required this.query});
  final String query;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final local = ref.watch(searchProvider(query));
    final catalog = ref.watch(catalogProvider(query));
    final shows = ref.watch(podcastSearchProvider(query));
    final albums = ref.watch(catalogAlbumsProvider(query));
    final player = ref.read(playerProvider);
    var n = 0;
    Widget fade(Widget w) => FadeSlideIn(index: n++, child: w);
    return ListView(padding: const EdgeInsets.only(bottom: 190), children: [
      ...local.when(
        loading: () => [const ListSkeleton(rows: 4)],
        error: (e, _) => [ErrorView(e)],
        data: (r) => [
          if (r.topArtist != null) fade(TopArtistCard(artist: r.topArtist!, onTap: () => push(context, ArtistScreen(r.topArtist!.name)))),
          if (_otherArtists(r).isNotEmpty)
            fade(SizedBox(
              height: 136,
              child: ListView.separated(
                scrollDirection: Axis.horizontal,
                padding: const EdgeInsets.fromLTRB(20, 8, 20, 0),
                itemCount: _otherArtists(r).length,
                separatorBuilder: (_, _) => const SizedBox(width: 14),
                itemBuilder: (context, i) => Pressable(
                  onTap: () => push(context, ArtistScreen(_otherArtists(r)[i].name)),
                  child: SizedBox(
                    width: 92,
                    child: Column(children: [
                      ArtistAvatar(_otherArtists(r)[i].coverUrl, size: 86),
                      const SizedBox(height: 6),
                      Text(_otherArtists(r)[i].name, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 12.5, fontWeight: FontWeight.w600)),
                    ]),
                  ),
                ),
              ),
            )),
          for (final a in r.albums.take(4))
            fade(ListTile(
              contentPadding: const EdgeInsets.symmetric(horizontal: 20),
              leading: Cover(a.coverUrl, icon: Icons.album_rounded),
              title: Text(a.name, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w600)),
              subtitle: Text('Album · ${a.artists.join(', ')}', maxLines: 1, overflow: TextOverflow.ellipsis),
              onTap: () => push(context, TrackListScreen.album(a.id, a.name)),
            )),
          for (var i = 0; i < r.tracks.length; i++) fade(TrackTile(track: r.tracks[i], list: r.tracks, onTap: () => player.play(r.tracks, index: i))),
          if (r.tracks.isEmpty && r.albums.isEmpty && r.artists.isEmpty)
            const Padding(padding: EdgeInsets.fromLTRB(20, 12, 20, 0), child: Text('Nothing in your library matches.', style: TextStyle(color: muted))),
        ],
      ),
      ...shows.when(
        loading: () => const [],
        error: (_, _) => const [],
        data: (list) => list.isEmpty
            ? const []
            : [
                const SectionTitle('Podcasts'),
                SizedBox(
                  height: 206,
                  child: ListView.separated(
                    scrollDirection: Axis.horizontal,
                    padding: const EdgeInsets.symmetric(horizontal: 20),
                    itemCount: list.length,
                    separatorBuilder: (_, _) => const SizedBox(width: 14),
                    itemBuilder: (context, i) => PodcastCard(podcast: list[i]),
                  ),
                ),
              ],
      ),
      ...albums.when(
        loading: () => const [],
        error: (_, _) => const [], // album search is extra: songs still show if it's unavailable
        data: (list) => list.isEmpty
            ? const []
            : [
                const SectionTitle('Albums', subtitle: 'Soundtracks, scores and more: add a whole album'),
                SizedBox(
                  height: 236,
                  child: ListView.separated(
                    scrollDirection: Axis.horizontal,
                    padding: const EdgeInsets.symmetric(horizontal: 20),
                    itemCount: list.length,
                    separatorBuilder: (_, _) => const SizedBox(width: 14),
                    itemBuilder: (context, i) => _CatalogAlbumCard(album: list[i]),
                  ),
                ),
              ],
      ),
      const SectionTitle('Not in your library?', subtitle: 'Tap to preview. Like it, and your server downloads it.'),
      ...catalog.when(
        loading: () => [const ListSkeleton(rows: 3)],
        error: (e, _) => [ErrorView(e)],
        data: (r) => [for (final item in r.$2.where((i) => i.inLibrary == null)) CatalogTile(item: item)],
      ),
    ]);
  }
}

class _CatalogAlbumCard extends StatelessWidget {
  const _CatalogAlbumCard({required this.album});
  final CatalogAlbum album;

  @override
  Widget build(BuildContext context) {
    final complete = album.count != null && album.count! > 0 && album.onServer >= album.count!;
    return Pressable(
      onTap: () => push(context, CatalogAlbumScreen(album)),
      child: SizedBox(
        width: 150,
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Stack(children: [
            Cover(album.thumbUrl, size: 150, radius: radiusM, icon: Icons.album_rounded),
            if (album.onServer > 0)
              Positioned(
                left: 8,
                top: 8,
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                  decoration: BoxDecoration(color: Colors.black.withValues(alpha: 0.6), borderRadius: BorderRadius.circular(10)),
                  child: Text(complete ? 'On your server' : '${album.onServer} on server',
                      style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w700)),
                ),
              ),
          ]),
          const SizedBox(height: 8),
          Text(album.name, maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13.5)),
          const SizedBox(height: 2),
          Text('${album.typeLabel} · ${album.artistLine}${album.count != null ? ' · ${album.count} songs' : ''}',
              maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 12)),
        ]),
      ),
    );
  }
}

/// Artists matching the search, minus the one already shown as the top result.
List<ArtistSummary> _otherArtists(SearchResults r) {
  final top = r.topArtist?.name.toLowerCase();
  return [for (final a in r.artists) if (a.name.toLowerCase() != top) a];
}

/// A song from Spotify/Deezer search or a chart: play it if the library has it, otherwise
/// preview it (and like or add it from there).
class CatalogTile extends ConsumerStatefulWidget {
  const CatalogTile({super.key, required this.item, this.rank});
  final CatalogItem item;
  final int? rank;
  @override
  ConsumerState<CatalogTile> createState() => _CatalogTileState();
}

class _CatalogTileState extends ConsumerState<CatalogTile> {
  bool _busy = false;
  String? _requestedStatus;

  Future<void> _request() async {
    final api = ref.read(apiProvider);
    if (api == null) return;
    setState(() => _busy = true);
    try {
      final t = await api.request(widget.item);
      setState(() => _requestedStatus = t.status);
      if (mounted) toast(context, t.playable ? 'Already in your library' : 'Added: your server is downloading it');
      ref.invalidate(requestsProvider);
      ref.invalidate(homeProvider);
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _play() async {
    final api = ref.read(apiProvider);
    final id = widget.item.inLibrary;
    if (api == null || id == null) return;
    try {
      final t = await api.track(id);
      await ref.read(playerProvider).play([t]);
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final item = widget.item;
    final status = _requestedStatus ?? item.status;
    final inLibrary = item.inLibrary != null || _requestedStatus != null;
    final ready = status == 'downloaded';
    return Pressable(
      scale: 0.98,
      onTap: ready ? _play : () => showPreview(context, ref, item: item),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(20, 7, 12, 7),
        child: Row(children: [
          if (widget.rank != null)
            SizedBox(
              width: 30,
              child: widget.rank! <= 3
                  ? GradientMask(child: Text('${widget.rank}', textAlign: TextAlign.center, style: const TextStyle(fontWeight: FontWeight.w900, fontSize: 16)))
                  : Text('${widget.rank}', textAlign: TextAlign.center, style: const TextStyle(color: muted)),
            ),
          if (widget.rank != null) const SizedBox(width: 8),
          Cover(item.thumbUrl, size: 50),
          const SizedBox(width: 14),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(item.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w600)),
              const SizedBox(height: 3),
              Text(inLibrary ? (ready ? item.artistLine : '${item.artistLine} · downloading to server') : item.artistLine,
                  maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 13)),
            ]),
          ),
          const SizedBox(width: 8),
          _busy
              ? const SizedBox(width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 2))
              : ready
                  ? IconButton(icon: const GradientMask(child: Icon(Icons.play_circle_fill_rounded, size: 34)), onPressed: _play)
                  : inLibrary
                      ? const Padding(padding: EdgeInsets.all(8), child: Icon(Icons.hourglass_top_rounded, color: muted))
                      : OutlinedButton(onPressed: _request, child: const Text('Add')),
        ]),
      ),
    );
  }
}

class GenreScreen extends ConsumerWidget {
  const GenreScreen(this.genre, {super.key});
  final Genre genre;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final chart = ref.watch(genreChartProvider(genre.id));
    return Scaffold(
      appBar: AppBar(
        leading: Padding(
          padding: const EdgeInsets.all(6),
          child: GlassIconButton(icon: Icons.arrow_back_rounded, onPressed: () => Navigator.maybePop(context)),
        ),
        title: Text(genre.id == 0 ? 'Top songs' : genre.name),
      ),
      body: RefreshIndicator(
        color: accent,
        backgroundColor: surface,
        onRefresh: () async => ref.invalidate(genreChartProvider(genre.id)),
        child: asyncView(chart, (items) {
          final missing = items.where((i) => i.inLibrary == null).toList();
          return ListView(padding: const EdgeInsets.only(bottom: 190), children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 4, 16, 8),
              child: Row(children: [
                Expanded(
                  child: Text('Popular right now · ${items.length - missing.length} of ${items.length} in your library',
                      style: const TextStyle(color: muted)),
                ),
                if (missing.isNotEmpty)
                  FilledButton.tonalIcon(
                    icon: const Icon(Icons.library_add_rounded),
                    label: Text('Add ${missing.length}'),
                    onPressed: () => _requestAll(context, ref, missing),
                  ),
              ]),
            ),
            for (var i = 0; i < items.length; i++)
              i < 14 ? FadeSlideIn(index: i, child: CatalogTile(item: items[i], rank: i + 1)) : CatalogTile(item: items[i], rank: i + 1),
          ]);
        }, onRetry: () => ref.invalidate(genreChartProvider(genre.id)), loading: const ListSkeleton()),
      ),
    );
  }

  Future<void> _requestAll(BuildContext context, WidgetRef ref, List<CatalogItem> items) async {
    final api = ref.read(apiProvider);
    if (api == null) return;
    final ok = await showDialog<bool>(
      context: context,
      builder: (d) => AlertDialog(
        title: Text('Add ${items.length} songs?'),
        content: const Text('Your server downloads them ahead of its backlog. They\'ll appear in the library as they finish.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(d, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(d, true), child: const Text('Add')),
        ],
      ),
    );
    if (ok != true) return;
    var added = 0;
    for (final item in items) {
      try {
        await api.request(item);
        added++;
      } on ApiException {
        // keep going: one unknown song shouldn't stop the rest
      }
    }
    ref.invalidate(genreChartProvider(genre.id));
    ref.invalidate(requestsProvider);
    if (context.mounted) toast(context, 'Added $added songs');
  }
}
