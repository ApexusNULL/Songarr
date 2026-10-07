import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../state/data.dart';
import '../state/player.dart';
import '../state/session.dart';
import 'fx/colors.dart';
import 'fx/motion.dart';
import 'preview_sheet.dart';
import 'theme.dart';
import 'widgets.dart';

/// A whole album from the catalogue (a soundtrack, a film score…): every track, which ones are on
/// your server already, and "Add album", which downloads the rest next, in album order.
class CatalogAlbumScreen extends ConsumerStatefulWidget {
  const CatalogAlbumScreen(this.album, {super.key});
  final CatalogAlbum album;

  @override
  ConsumerState<CatalogAlbumScreen> createState() => _CatalogAlbumScreenState();
}

class _CatalogAlbumScreenState extends ConsumerState<CatalogAlbumScreen> {
  Timer? _refresh;
  bool _busy = false;

  String get _key => '${widget.album.source}:${widget.album.id}';

  @override
  void dispose() {
    _refresh?.cancel();
    super.dispose();
  }

  /// While songs are downloading, check now and then so they turn playable as they arrive.
  void _watch(CatalogAlbumPage page) {
    final waiting = page.tracks.any((t) => t.inLibrary != null && t.status != 'downloaded');
    if (waiting && _refresh == null) {
      _refresh = Timer.periodic(const Duration(seconds: 20), (_) => ref.invalidate(catalogAlbumProvider(_key)));
    } else if (!waiting) {
      _refresh?.cancel();
      _refresh = null;
    }
  }

  Future<void> _add(CatalogAlbumPage page) async {
    final api = ref.read(apiProvider);
    if (api == null) return;
    final missing = page.count - page.onServer;
    if (missing > 40) {
      final ok = await showDialog<bool>(
        context: context,
        builder: (d) => AlertDialog(
          title: Text('Add all ${page.count} songs?'),
          content: Text('$missing of them aren\'t on your server yet. They download next, ahead of the backlog, in album order.'),
          actions: [
            TextButton(onPressed: () => Navigator.pop(d, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(d, true), child: const Text('Add album')),
          ],
        ),
      );
      if (ok != true) return;
    }
    setState(() => _busy = true);
    try {
      final done = await api.requestAlbum(widget.album.source, widget.album.id);
      ref.invalidate(catalogAlbumProvider(_key));
      ref.invalidate(requestsProvider);
      ref.invalidate(homeProvider);
      if (mounted) {
        final queued = done.count - done.onServer;
        toast(context, queued == 0 ? 'Added: every song is already on your server' : 'Added: downloading $queued songs next');
      }
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _play(CatalogAlbumPage page, CatalogItem item) {
    final ready = [for (final t in page.tracks) if (t.status == 'downloaded' && t.track != null) t.track!];
    final i = ready.indexWhere((t) => t.id == item.inLibrary);
    if (ready.isNotEmpty) ref.read(playerProvider).play(ready, index: i < 0 ? 0 : i);
  }

  @override
  Widget build(BuildContext context) {
    final value = ref.watch(catalogAlbumProvider(_key));
    final a = widget.album;
    final tint = a.coverUrl == null ? null : ref.watch(artColorProvider(a.coverUrl!)).value;
    value.whenData(_watch);
    return Scaffold(
      appBar: AppBar(
        leading: Padding(padding: const EdgeInsets.all(6), child: GlassIconButton(icon: Icons.arrow_back_rounded, onPressed: () => Navigator.maybePop(context))),
      ),
      extendBodyBehindAppBar: true,
      body: RefreshIndicator(
        color: accent,
        backgroundColor: surface,
        onRefresh: () async => ref.invalidate(catalogAlbumProvider(_key)),
        child: ListView(padding: const EdgeInsets.only(bottom: 190), children: [
          SafeArea(
            bottom: false,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(24, 56, 24, 8),
              child: Column(children: [
                FadeSlideIn(
                  child: Container(
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(radiusL),
                      boxShadow: [BoxShadow(color: (tint ?? accent).withValues(alpha: 0.45), blurRadius: 50, spreadRadius: 2)],
                    ),
                    child: Cover(a.coverUrl, size: 210, radius: radiusL, icon: Icons.album_rounded),
                  ),
                ),
                const SizedBox(height: 20),
                Text(a.name, textAlign: TextAlign.center, maxLines: 3, overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.headlineMedium),
                const SizedBox(height: 6),
                Text(a.artistLine, textAlign: TextAlign.center, style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 15)),
                const SizedBox(height: 4),
                Text(
                  value.whenOrNull(data: (p) => p.subtitle) ?? '${a.typeLabel}${a.count != null ? ' · ${a.count} songs' : ''}',
                  textAlign: TextAlign.center,
                  style: const TextStyle(color: muted, fontSize: 13),
                ),
                const SizedBox(height: 18),
                ...value.when(
                  loading: () => [const SizedBox(height: 52, child: Center(child: CircularProgressIndicator()))],
                  error: (e, _) => [ErrorView(e, onRetry: () => ref.invalidate(catalogAlbumProvider(_key)))],
                  data: (p) => [_AddButton(page: p, busy: _busy, onAdd: () => _add(p), onPlay: () => _play(p, p.tracks.first))],
                ),
              ]),
            ),
          ),
          ...value.maybeWhen(
            data: (p) => [
              for (var i = 0; i < p.tracks.length; i++) ...[
                if (p.discs > 1 && (i == 0 || p.tracks[i].discNumber != p.tracks[i - 1].discNumber))
                  Padding(
                    padding: const EdgeInsets.fromLTRB(20, 18, 20, 4),
                    child: Text('Disc ${p.tracks[i].discNumber}', style: const TextStyle(color: muted, fontWeight: FontWeight.w800, letterSpacing: 1)),
                  ),
                _AlbumTrackRow(
                  item: p.tracks[i],
                  albumArtist: a.artistLine,
                  onTap: () => p.tracks[i].status == 'downloaded' ? _play(p, p.tracks[i]) : showPreview(context, ref, item: p.tracks[i]),
                ),
              ],
              const Padding(
                padding: EdgeInsets.fromLTRB(20, 18, 20, 0),
                child: Text('Album details from Deezer. Tap a song that isn\'t on your server to hear a preview.',
                    style: TextStyle(color: muted, fontSize: 12)),
              ),
            ],
            orElse: () => const [],
          ),
        ]),
      ),
    );
  }
}

class _AddButton extends StatelessWidget {
  const _AddButton({required this.page, required this.busy, required this.onAdd, required this.onPlay});
  final CatalogAlbumPage page;
  final bool busy;
  final VoidCallback onAdd;
  final VoidCallback onPlay;

  @override
  Widget build(BuildContext context) {
    final waiting = page.tracks.where((t) => t.inLibrary != null && t.status != 'downloaded').length;
    final missing = page.count - page.onServer;
    final String label;
    final IconData icon;
    VoidCallback? action;
    if (busy) {
      label = 'Adding…';
      icon = Icons.hourglass_top_rounded;
    } else if (!page.added) {
      label = missing == 0 ? 'Add album' : 'Add album · $missing new songs';
      icon = Icons.library_add_rounded;
      action = onAdd;
    } else if (waiting > 0) {
      label = 'Downloading · ${page.onServer} of ${page.count} ready';
      icon = Icons.downloading_rounded;
      action = page.onServer > 0 ? onPlay : null;
    } else {
      label = 'In your library · Play';
      icon = Icons.play_arrow_rounded;
      action = onPlay;
    }
    return Pressable(
      onTap: action,
      child: AnimatedOpacity(
        duration: const Duration(milliseconds: 200),
        opacity: action == null && !busy ? 0.7 : 1,
        child: Container(
          height: 52,
          padding: const EdgeInsets.symmetric(horizontal: 22),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(26),
            gradient: page.added ? null : aurora,
            color: page.added ? glassFill : null,
            border: page.added ? Border.all(color: glassBorder) : null,
            boxShadow: page.added ? null : [BoxShadow(color: accent2.withValues(alpha: 0.4), blurRadius: 20)],
          ),
          child: Row(mainAxisSize: MainAxisSize.min, children: [
            Icon(icon, size: 22),
            const SizedBox(width: 10),
            Text(label, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 15.5)),
          ]),
        ),
      ),
    );
  }
}

class _AlbumTrackRow extends StatelessWidget {
  const _AlbumTrackRow({required this.item, required this.albumArtist, required this.onTap});
  final CatalogItem item;
  final String albumArtist;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final ready = item.status == 'downloaded';
    final waiting = item.inLibrary != null && !ready;
    final sub = [
      if (item.artistLine != albumArtist) item.artistLine,
      if (waiting) 'downloading to server',
      formatDuration(item.duration),
    ].join(' · ');
    return Pressable(
      scale: 0.98,
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(20, 8, 16, 8),
        child: Row(children: [
          SizedBox(
            width: 32,
            child: Text('${item.trackNumber ?? ''}', textAlign: TextAlign.center, style: const TextStyle(color: muted, fontWeight: FontWeight.w600)),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(item.title, maxLines: 2, overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontWeight: FontWeight.w600, color: ready || waiting ? null : Colors.white.withValues(alpha: 0.85))),
              const SizedBox(height: 2),
              Text(sub, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 12.5)),
            ]),
          ),
          const SizedBox(width: 8),
          if (ready)
            const GradientMask(child: Icon(Icons.check_circle_rounded, size: 22))
          else if (waiting)
            const Icon(Icons.hourglass_top_rounded, color: muted, size: 20)
          else
            const Icon(Icons.play_circle_outline_rounded, color: muted, size: 22),
        ]),
      ),
    );
  }
}
