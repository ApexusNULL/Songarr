import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../state/data.dart';
import '../state/session.dart';
import 'fx/motion.dart';
import 'theme.dart';
import 'widgets.dart';

/// Drag playlists into the order you want in your library. The order is saved on your server,
/// so it's the same on all your devices (and on Home).
class ArrangePlaylistsScreen extends ConsumerStatefulWidget {
  const ArrangePlaylistsScreen({super.key, required this.playlists, this.focus});
  final List<PlaylistSummary> playlists;
  final String? focus; // the playlist that was long-pressed to get here

  @override
  ConsumerState<ArrangePlaylistsScreen> createState() => _ArrangePlaylistsScreenState();
}

class _ArrangePlaylistsScreenState extends ConsumerState<ArrangePlaylistsScreen> {
  late final _items = [...widget.playlists];
  // taken when the screen opens: saving and refreshing also happen as it closes, when ref can't be used
  late final SongarrApi? _api;
  late final ProviderContainer _container;
  Timer? _saveSoon;
  bool _dirty = false;

  @override
  void initState() {
    super.initState();
    _api = ref.read(apiProvider);
    _container = ProviderScope.containerOf(context, listen: false);
  }

  @override
  void dispose() {
    _saveSoon?.cancel();
    _save(); // anything not yet saved
    super.dispose();
  }

  void _changed() {
    HapticFeedback.selectionClick();
    setState(() => _dirty = true);
    _saveSoon?.cancel();
    _saveSoon = Timer(const Duration(milliseconds: 800), _save);
  }

  Future<void> _save() async {
    if (!_dirty || _api == null) return;
    _dirty = false;
    try {
      await _api.setPlaylistOrder([for (final p in _items) p.id]);
      _container.invalidate(libraryProvider);
      _container.invalidate(homeProvider);
    } on ApiException catch (e) {
      _dirty = true;
      if (mounted) toast(context, 'Couldn\'t save the order: ${e.message}');
    }
  }

  void _move(int from, int to) {
    final p = _items.removeAt(from);
    _items.insert(to, p);
    _changed();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: Padding(padding: const EdgeInsets.all(6), child: GlassIconButton(icon: Icons.arrow_back_rounded, onPressed: () => Navigator.maybePop(context))),
        title: const Text('Arrange playlists'),
        actions: [
          TextButton(
            onPressed: () {
              _items.sort((a, b) => a.name.toLowerCase().compareTo(b.name.toLowerCase()));
              _changed();
            },
            child: const Text('A–Z'),
          ),
          TextButton(onPressed: () => Navigator.maybePop(context), child: const Text('Done', style: TextStyle(fontWeight: FontWeight.w800))),
          const SizedBox(width: 6),
        ],
      ),
      body: Column(children: [
        const Padding(
          padding: EdgeInsets.fromLTRB(20, 4, 20, 10),
          child: Text('Drag a playlist by its handle (or press and hold it) to move it. You can also press and hold a playlist on Home and drag it there. Your order is kept on your server, so it\'s the same on all your devices.',
              style: TextStyle(color: muted, height: 1.35)),
        ),
        Expanded(
          child: ReorderableListView.builder(
            buildDefaultDragHandles: false,
            padding: const EdgeInsets.only(bottom: 190),
            itemCount: _items.length,
            onReorderItem: _move,
            proxyDecorator: (child, _, animation) => AnimatedBuilder(
              animation: animation,
              builder: (context, child) {
                final t = Curves.easeOutCubic.transform(animation.value);
                return Transform.scale(
                  scale: 1 + 0.03 * t,
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(radiusM),
                      color: surface,
                      border: Border.all(color: accent.withValues(alpha: 0.5 * t)),
                      boxShadow: [BoxShadow(color: accent2.withValues(alpha: 0.35 * t), blurRadius: 24)],
                    ),
                    child: Material(type: MaterialType.transparency, child: child),
                  ),
                );
              },
              child: child,
            ),
            itemBuilder: (context, i) {
              final p = _items[i];
              return ReorderableDelayedDragStartListener(
                key: ValueKey(p.id),
                index: i,
                child: _ArrangeRow(
                  playlist: p,
                  index: i,
                  highlight: p.id == widget.focus,
                  onTop: i == 0 ? null : () => _move(i, 0),
                ),
              );
            },
          ),
        ),
      ]),
    );
  }
}

class _ArrangeRow extends StatelessWidget {
  const _ArrangeRow({required this.playlist, required this.index, required this.highlight, required this.onTop});
  final PlaylistSummary playlist;
  final int index;
  final bool highlight;
  final VoidCallback? onTop;

  @override
  Widget build(BuildContext context) {
    final p = playlist;
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 10, vertical: 2),
      padding: const EdgeInsets.fromLTRB(10, 6, 4, 6),
      decoration: highlight
          ? BoxDecoration(borderRadius: BorderRadius.circular(radiusM), border: Border.all(color: accent.withValues(alpha: 0.6)), color: glassFill)
          : null,
      child: Row(children: [
        Cover(p.imageUrl, size: 52, icon: Icons.queue_music_rounded),
        const SizedBox(width: 12),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(p.name, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 15)),
            const SizedBox(height: 2),
            Text('${p.origin} · ${p.count} songs',
                maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 12.5)),
          ]),
        ),
        IconButton(
          tooltip: 'Move to the top',
          icon: Icon(Icons.vertical_align_top_rounded, color: onTop == null ? Colors.white24 : Colors.white70),
          onPressed: onTop,
        ),
        ReorderableDragStartListener(
          index: index,
          child: const Padding(
            padding: EdgeInsets.all(10),
            child: GradientMask(child: Icon(Icons.drag_handle_rounded, size: 26)),
          ),
        ),
      ]),
    );
  }
}
