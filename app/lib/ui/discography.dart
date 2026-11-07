import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../state/data.dart';
import '../state/session.dart';
import 'catalog_album_screen.dart';
import 'fx/clock.dart';
import 'fx/motion.dart';
import 'theme.dart';
import 'widgets.dart';

/// An artist's discography on their page: everything they've released (from the catalogue), each
/// release to open and add on its own, and "Add all" to add the lot in one go.
class DiscographySection extends ConsumerStatefulWidget {
  const DiscographySection({super.key, required this.deezerId, required this.name});
  final int deezerId;
  final String name;

  @override
  ConsumerState<DiscographySection> createState() => _DiscographySectionState();
}

class _DiscographySectionState extends ConsumerState<DiscographySection> {
  Timer? _refresh;
  bool _singles = false; // the "Singles and EPs" pill, else albums

  String get _key => '${widget.deezerId}:${widget.name}';

  @override
  void initState() {
    super.initState();
    ref.listenManual(discographyProvider(_key), (_, next) => next.whenData(_watch), fireImmediately: true);
  }

  @override
  void dispose() {
    _refresh?.cancel();
    super.dispose();
  }

  /// While the server is adding it, look again every few seconds; once it's done, the artist's songs
  /// on the server, the requests and the bell have all changed.
  void _watch(Discography d) {
    if (d.adding != null && _refresh == null) {
      _refresh = Timer.periodic(const Duration(seconds: 3), (_) {
        if (mounted && showing(context)) ref.invalidate(discographyProvider(_key)); // (not unseen, in the background)
      });
    } else if (d.adding == null && _refresh != null) {
      _refresh!.cancel();
      _refresh = null;
      for (final p in [notificationsProvider, requestsProvider, homeProvider]) {
        ref.invalidate(p);
      }
      ref.invalidate(artistProvider(widget.name));
    }
  }

  static String _summary(List<CatalogAlbum> releases) {
    String count(String type, String one, String many) {
      final n = releases.where((r) => r.type == type).length;
      return n == 0 ? '' : '$n ${n == 1 ? one : many}';
    }

    return [count('album', 'album', 'albums'), count('ep', 'EP', 'EPs'), count('single', 'single', 'singles'),
            count('compile', 'compilation', 'compilations')]
        .where((s) => s.isNotEmpty)
        .join(' · ');
  }

  @override
  Widget build(BuildContext context) {
    final d = ref.watch(discographyProvider(_key)).value;
    if (d == null || d.releases.isEmpty) return const SizedBox(); // still looking, the catalogue is away, or nothing
    final albums = [for (final r in d.releases) if (r.type == 'album' || r.type == 'compile') r];
    final singles = [for (final r in d.releases) if (r.type == 'single' || r.type == 'ep') r];
    final shown = (_singles && singles.isNotEmpty) || albums.isEmpty ? singles : albums;
    final adding = d.adding;
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      SectionTitle(
        'Discography',
        subtitle: adding == null
            ? _summary(d.releases)
            : 'Adding… looked through ${adding.done} of ${adding.of} releases, ${adding.songs} songs so far',
        action: adding == null ? 'Add all' : null,
        onAction: () => showAddDiscography(context, deezerId: widget.deezerId, name: widget.name, releases: d.releases),
      ),
      if (albums.isNotEmpty && singles.isNotEmpty)
        SingleChildScrollView(
          scrollDirection: Axis.horizontal, // (large text on a narrow phone)
          padding: const EdgeInsets.fromLTRB(20, 0, 20, 12),
          child: Row(children: [
            FilterPill(label: 'Albums', selected: !_singles, onTap: () => setState(() => _singles = false)),
            const SizedBox(width: 8),
            FilterPill(label: 'Singles and EPs', selected: _singles, onTap: () => setState(() => _singles = true)),
          ]),
        ),
      SizedBox(
        height: 206,
        child: ListView.separated(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.symmetric(horizontal: 20),
          itemCount: shown.length,
          separatorBuilder: (_, _) => const SizedBox(width: 14),
          itemBuilder: (context, i) {
            final r = shown[i];
            return Pressable(
              onTap: () => push(context, CatalogAlbumScreen(r)),
              child: SizedBox(
                width: 140,
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Cover(r.coverUrl, size: 140, radius: radiusM, icon: Icons.album_rounded),
                  const SizedBox(height: 8),
                  Text(r.name, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700)),
                  Text([?r.year, r.typeLabel].join(' · '), style: const TextStyle(color: muted, fontSize: 12)),
                ]),
              ),
            );
          },
        ),
      ),
    ]);
  }
}

/// Pick which of an artist's releases to add (albums and EPs to start with), then add them all.
Future<void> showAddDiscography(BuildContext context,
        {required int deezerId, required String name, required List<CatalogAlbum> releases}) =>
    showModalBottomSheet<void>(
      context: context,
      useRootNavigator: true, // above the mini player and tabs
      isScrollControlled: true,
      builder: (_) => _AddDiscographySheet(deezerId: deezerId, name: name, releases: releases),
    );

class _AddDiscographySheet extends ConsumerStatefulWidget {
  const _AddDiscographySheet({required this.deezerId, required this.name, required this.releases});
  final int deezerId;
  final String name;
  final List<CatalogAlbum> releases;

  @override
  ConsumerState<_AddDiscographySheet> createState() => _AddDiscographySheetState();
}

class _AddDiscographySheetState extends ConsumerState<_AddDiscographySheet> {
  // Singles are mostly songs from the albums, and hits albums (Deezer calls most of them albums)
  // only add what no album has. Live albums are albums to Deezer too: for a band that tours, half
  // their songs. So albums and EPs to start with, and the rest a tick away.
  late final Set<String> _picked = {for (final r in widget.releases) if (_kind(r) == 'album' || _kind(r) == 'ep') r.id};
  bool _busy = false;

  static const _kinds = [('album', 'Albums'), ('live', 'Live albums'), ('ep', 'EPs'), ('single', 'Singles'), ('compile', 'Compilations')];
  static final _liveWord = RegExp(r'\blive\b', caseSensitive: false);

  static String _kind(CatalogAlbum r) => r.type == 'album' && _liveWord.hasMatch(r.name) ? 'live' : r.type;

  Future<void> _add() async {
    final api = ref.read(apiProvider);
    if (api == null || _picked.isEmpty) return;
    setState(() => _busy = true);
    try {
      final n = _picked.length;
      await api.addDiscography(widget.deezerId, widget.name, [for (final r in widget.releases) if (_picked.contains(r.id)) r.id]);
      ref.invalidate(discographyProvider('${widget.deezerId}:${widget.name}'));
      if (mounted) {
        Navigator.pop(context);
        toast(context, 'Adding $n release${n == 1 ? '' : 's'}. A notification says when the songs are on their way.');
      }
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final groups = [
      for (final (kind, label) in _kinds)
        if (widget.releases.any((r) => _kind(r) == kind)) (label, [for (final r in widget.releases) if (_kind(r) == kind) r]),
    ];
    final n = _picked.length;
    return DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.8,
      maxChildSize: 0.95,
      builder: (context, scroll) => SafeArea(
        child: Column(children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text('Add ${widget.name}\'s discography', style: Theme.of(context).textTheme.titleLarge),
              const SizedBox(height: 4),
              const Text('A song on several releases downloads once, and songs already on your server just join your library.',
                  style: TextStyle(color: muted)),
            ]),
          ),
          Expanded(
            child: ListView(controller: scroll, children: [
              for (final (label, list) in groups) ...[
                CheckboxListTile(
                  tristate: true,
                  value: list.every((r) => _picked.contains(r.id)) ? true : (list.any((r) => _picked.contains(r.id)) ? null : false),
                  onChanged: (_) => setState(() {
                    final all = list.every((r) => _picked.contains(r.id));
                    for (final r in list) {
                      all ? _picked.remove(r.id) : _picked.add(r.id);
                    }
                  }),
                  activeColor: accent,
                  title: Text('$label (${list.length})', style: const TextStyle(fontWeight: FontWeight.w800)),
                ),
                for (final r in list)
                  CheckboxListTile(
                    value: _picked.contains(r.id),
                    onChanged: (on) => setState(() => on == true ? _picked.add(r.id) : _picked.remove(r.id)),
                    activeColor: accent,
                    secondary: Cover(r.thumbUrl, icon: Icons.album_rounded),
                    title: Text(r.name, maxLines: 1, overflow: TextOverflow.ellipsis),
                    subtitle: Text([?r.year, if (r.onServer > 0) '${r.onServer} on your server'].join(' · '),
                        style: const TextStyle(color: muted)),
                  ),
              ],
            ]),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 12),
            child: GradientButton(
              label: _busy ? 'Adding…' : (n == 0 ? 'Pick something to add' : 'Add $n release${n == 1 ? '' : 's'}'),
              onTap: _busy || n == 0 ? null : _add,
            ),
          ),
        ]),
      ),
    );
  }
}
