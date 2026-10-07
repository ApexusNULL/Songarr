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

/// Drag the songs of one of your own playlists (or your Liked Songs) into a new order. Each move
/// is saved as you go.
class ReorderSongsScreen extends ConsumerStatefulWidget {
  const ReorderSongsScreen({super.key, required this.playlistId, required this.name, required this.tracks, this.focus});
  const ReorderSongsScreen.liked({super.key, required this.tracks, this.focus})
      : playlistId = null,
        name = 'Liked Songs';
  final String? playlistId; // null: Liked Songs
  final String name;
  final List<Track> tracks;
  final int? focus; // position of the song you came from

  @override
  ConsumerState<ReorderSongsScreen> createState() => _ReorderSongsScreenState();
}

/// A song in the list, with a key of its own (a playlist can hold the same song twice).
class _Entry {
  _Entry(this.track, this.key);
  final Track track;
  final int key;
}

class _ReorderSongsScreenState extends ConsumerState<ReorderSongsScreen> {
  late List<_Entry> _items = _entries(widget.tracks);
  // taken when the screen opens: saving and refreshing also happen as it closes, when ref can't be used
  late final SongarrApi? _api;
  late final ProviderContainer _container;
  late final _scroll = ScrollController(initialScrollOffset: ((widget.focus ?? 0) - 3).clamp(0, 1 << 20) * 64.0);
  Future<void> _saving = Future.value();
  int _serial = 0;

  @override
  void initState() {
    super.initState();
    _api = ref.read(apiProvider);
    _container = ProviderScope.containerOf(context, listen: false);
  }

  List<_Entry> _entries(List<Track> tracks) => [for (final t in tracks) _Entry(t, _serial++)];

  @override
  void dispose() {
    _scroll.dispose();
    final pid = widget.playlistId;
    _saving.whenComplete(() => _container.invalidate(pid == null ? likedProvider : playlistProvider(pid)));
    super.dispose();
  }

  void _move(int from, int to) {
    if (from == to) return;
    HapticFeedback.selectionClick();
    final moved = _items[from].track;
    setState(() => _items.insert(to, _items.removeAt(from)));
    // one at a time, in order, so the server sees the same moves you made
    _saving = _saving.then((_) async {
      try {
        final pid = widget.playlistId;
        await (pid == null ? _api?.moveInLiked(from, to, moved.id) : _api?.moveInPlaylist(pid, from, to, moved.id));
      } on ApiException catch (e) {
        if (!mounted) return;
        toast(context, e.message);
        final pid = widget.playlistId;
        final fresh = await (pid == null ? _api?.liked() : _api?.playlist(pid));
        if (fresh != null && mounted) setState(() => _items = _entries(fresh.tracks));
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: Padding(padding: const EdgeInsets.all(6), child: GlassIconButton(icon: Icons.arrow_back_rounded, onPressed: () => Navigator.maybePop(context))),
        title: Text(widget.name.isEmpty ? 'Reorder songs' : widget.name, maxLines: 1, overflow: TextOverflow.ellipsis),
        actions: [
          TextButton(onPressed: () => Navigator.maybePop(context), child: const Text('Done', style: TextStyle(fontWeight: FontWeight.w800))),
          const SizedBox(width: 6),
        ],
      ),
      body: Column(children: [
        const Padding(
          padding: EdgeInsets.fromLTRB(20, 4, 20, 10),
          child: Text('Drag a song by its handle (or press and hold it) to move it. Changes save as you go.',
              style: TextStyle(color: muted, height: 1.35)),
        ),
        Expanded(
          child: ReorderableListView.builder(
            scrollController: _scroll,
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
              final e = _items[i];
              final t = e.track;
              return ReorderableDelayedDragStartListener(
                key: ValueKey(e.key),
                index: i,
                child: Container(
                  height: 64,
                  margin: const EdgeInsets.symmetric(horizontal: 10),
                  padding: const EdgeInsets.only(left: 10),
                  decoration: i == widget.focus
                      ? BoxDecoration(borderRadius: BorderRadius.circular(radiusM), border: Border.all(color: accent.withValues(alpha: 0.6)), color: glassFill)
                      : null,
                  child: Row(children: [
                    Cover(t.thumbUrl, size: 46),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(mainAxisAlignment: MainAxisAlignment.center, crossAxisAlignment: CrossAxisAlignment.start, children: [
                        Text(t.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700)),
                        Text(t.artistLine, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 12.5)),
                      ]),
                    ),
                    IconButton(
                      tooltip: 'Move to the top',
                      icon: Icon(Icons.vertical_align_top_rounded, color: i == 0 ? Colors.white24 : Colors.white70),
                      onPressed: i == 0 ? null : () => _move(i, 0),
                    ),
                    ReorderableDragStartListener(
                      index: i,
                      child: const Padding(padding: EdgeInsets.all(10), child: GradientMask(child: Icon(Icons.drag_handle_rounded, size: 26))),
                    ),
                  ]),
                ),
              );
            },
          ),
        ),
      ]),
    );
  }
}
