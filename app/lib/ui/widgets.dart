import 'dart:math';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:just_audio/just_audio.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../state/data.dart';
import '../state/jam.dart';
import '../state/offline.dart';
import '../state/player.dart';
import '../state/session.dart';
import 'fx/motion.dart';
import 'fx/sparkle.dart';
import 'jam_sheet.dart';
import 'player_screen.dart';
import 'podcast_screen.dart';
import 'reorder_songs_screen.dart';
import 'theme.dart';
import 'tracklist_screen.dart';

String formatDuration(Duration d) {
  final h = d.inHours, m = d.inMinutes % 60, s = d.inSeconds % 60;
  return h > 0 ? '$h:${m.toString().padLeft(2, '0')}:${s.toString().padLeft(2, '0')}' : '$m:${s.toString().padLeft(2, '0')}';
}

/// "1 h 5 min" / "42 min" for episode lengths.
String formatLength(Duration d) {
  if (d.inMinutes >= 60) return '${d.inHours} h ${d.inMinutes % 60} min';
  return '${max(1, d.inMinutes)} min';
}

String formatDate(DateTime? d) {
  if (d == null) return '';
  final days = DateTime.now().difference(d).inDays;
  if (days < 1) return 'Today';
  if (days < 2) return 'Yesterday';
  if (days < 7) return '$days days ago';
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return '${months[d.month - 1]} ${d.day}${d.year == DateTime.now().year ? '' : ', ${d.year}'}';
}

String formatBytes(int bytes) {
  if (bytes >= 1 << 30) return '${(bytes / (1 << 30)).toStringAsFixed(1)} GB';
  if (bytes >= 1 << 20) return '${(bytes / (1 << 20)).toStringAsFixed(0)} MB';
  return '${(bytes / 1024).toStringAsFixed(0)} KB';
}

void push(BuildContext context, Widget page) => Navigator.of(context).push(MaterialPageRoute(builder: (_) => page));

void toast(BuildContext context, String message) =>
    ScaffoldMessenger.maybeOf(context)?.showSnackBar(SnackBar(content: Text(message), duration: const Duration(seconds: 3)));

class Cover extends StatelessWidget {
  const Cover(this.url, {super.key, this.size = 48, this.radius = radiusS, this.icon = Icons.music_note_rounded});
  final String? url;
  final double size;
  final double radius;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    final placeholder = Container(
      width: size,
      height: size,
      decoration: const BoxDecoration(
        gradient: LinearGradient(colors: [surfaceHigh, surface], begin: Alignment.topLeft, end: Alignment.bottomRight),
      ),
      child: Icon(icon, color: muted, size: size * 0.42),
    );
    return ClipRRect(
      borderRadius: BorderRadius.circular(radius),
      child: url == null
          ? placeholder
          : CachedNetworkImage(
              imageUrl: url!,
              width: size,
              height: size,
              fit: BoxFit.cover,
              fadeInDuration: const Duration(milliseconds: 250),
              placeholder: (_, _) => placeholder,
              errorWidget: (_, _, _) => placeholder,
              memCacheWidth: (size * MediaQuery.devicePixelRatioOf(context)).round(),
            ),
    );
  }
}

/// Liked Songs' cover: just the white heart, glowing softly on glass.
class LikedCover extends StatelessWidget {
  const LikedCover({super.key, this.size = 56, this.radius = radiusS});
  final double size;
  final double radius;

  @override
  Widget build(BuildContext context) => Container(
        width: size,
        height: size,
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(radius),
          border: Border.all(color: glassBorder),
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: [Colors.white.withValues(alpha: 0.12), Colors.white.withValues(alpha: 0.03)],
          ),
        ),
        child: Icon(
          Icons.favorite_rounded,
          color: Colors.white,
          size: size * 0.46,
          shadows: [Shadow(color: Colors.white.withValues(alpha: 0.55), blurRadius: size * 0.25)],
        ),
      );
}

/// A round artist photo inside an aurora ring.
class ArtistAvatar extends StatelessWidget {
  const ArtistAvatar(this.url, {super.key, this.size = 120, this.ring = true});
  final String? url;
  final double size;
  final bool ring;

  @override
  Widget build(BuildContext context) {
    final inner = ClipOval(
      child: url == null
          ? Container(
              color: surfaceHigh,
              child: Icon(Icons.person_rounded, color: muted, size: size * 0.45),
            )
          : CachedNetworkImage(
              imageUrl: url!,
              fit: BoxFit.cover,
              fadeInDuration: const Duration(milliseconds: 250),
              memCacheWidth: (size * MediaQuery.devicePixelRatioOf(context)).round(),
              errorWidget: (_, _, _) => Container(color: surfaceHigh, child: Icon(Icons.person_rounded, color: muted, size: size * 0.45)),
            ),
    );
    return Container(
      width: size,
      height: size,
      padding: EdgeInsets.all(ring ? 2.5 : 0),
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        gradient: ring ? const SweepGradient(colors: [accent, accent2, accent3, accent]) : null,
        boxShadow: ring ? [BoxShadow(color: accent.withValues(alpha: 0.25), blurRadius: 18)] : null,
      ),
      child: Container(
        padding: EdgeInsets.all(ring ? 2 : 0),
        decoration: const BoxDecoration(shape: BoxShape.circle, color: background),
        child: SizedBox.expand(child: inner),
      ),
    );
  }
}

/// A small pill with a gradient edge, e.g. "Because you like …".
class ReasonChip extends StatelessWidget {
  const ReasonChip(this.text, {super.key, this.icon = Icons.auto_awesome_rounded});
  final String text;
  final IconData icon;

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(20),
          gradient: LinearGradient(colors: [accent.withValues(alpha: 0.28), accent2.withValues(alpha: 0.18)]),
          border: Border.all(color: accent.withValues(alpha: 0.4)),
        ),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Icon(icon, size: 13, color: glowColor),
          const SizedBox(width: 5),
          Flexible(child: Text(text, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 12, color: Colors.white))),
        ]),
      );
}

class Loading extends StatelessWidget {
  const Loading({super.key});
  @override
  Widget build(BuildContext context) => const Center(child: Padding(padding: EdgeInsets.all(32), child: CircularProgressIndicator()));
}

/// Placeholder rows while a list loads.
class ListSkeleton extends StatelessWidget {
  const ListSkeleton({super.key, this.rows = 8});
  final int rows;

  @override
  Widget build(BuildContext context) => Column(children: [
        for (var i = 0; i < rows; i++)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
            child: Row(children: [
              const ShimmerBox(width: 50, height: 50, radius: radiusS),
              const SizedBox(width: 14),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  ShimmerBox(width: 120.0 + (i * 37) % 90, height: 13, radius: 6),
                  const SizedBox(height: 8),
                  ShimmerBox(width: 80.0 + (i * 53) % 60, height: 11, radius: 6),
                ]),
              ),
            ]),
          ),
      ]);
}

class ErrorView extends ConsumerWidget {
  const ErrorView(this.error, {super.key, this.onRetry});
  final Object error;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final e = error;
    if (e is ApiException && e.unauthorized) {
      // The device was signed out from Songarr's People page.
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const Icon(Icons.lock_outline_rounded, size: 40, color: muted),
            const SizedBox(height: 12),
            const Text('This device was signed out on the server.', textAlign: TextAlign.center),
            const SizedBox(height: 16),
            FilledButton(
              onPressed: () async {
                await ref.read(playerProvider).stop();
                await ref.read(sessionProvider.notifier).signOut(revoke: false);
              },
              child: const Text('Sign in again'),
            ),
          ]),
        ),
      );
    }
    final offline = e is ApiException && e.status == null;
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Icon(offline ? Icons.cloud_off_rounded : Icons.error_outline_rounded, size: 40, color: muted),
          const SizedBox(height: 12),
          Text(error.toString(), textAlign: TextAlign.center),
          if (offline) ...[
            const SizedBox(height: 6),
            const Text('Downloaded songs still play: Library → Downloads.', style: TextStyle(color: muted), textAlign: TextAlign.center),
          ],
          if (onRetry != null) ...[
            const SizedBox(height: 16),
            OutlinedButton(onPressed: onRetry, child: const Text('Try again')),
          ],
        ]),
      ),
    );
  }
}

/// AsyncValue -> loading / error / data, with retry.
Widget asyncView<T>(AsyncValue<T> value, Widget Function(T data) builder, {VoidCallback? onRetry, Widget? loading}) => value.when(
      data: builder,
      loading: () => loading ?? const Loading(),
      error: (e, _) => ErrorView(e, onRetry: onRetry),
    );

class TrackTile extends ConsumerWidget {
  const TrackTile(
      {super.key,
      required this.track,
      required this.onTap,
      this.number,
      this.showCover = true,
      this.playlistId,
      this.position,
      this.list,
      this.inLiked = false});
  final bool inLiked; // shown in Liked Songs: "Move in Liked Songs"
  final Track track;
  final List<Track>? list; // the list it's shown in, for "Start a Jam with this"
  final VoidCallback onTap;
  final int? number;
  final bool showCover;
  final String? playlistId; // set for app playlists, enables "Remove from playlist"
  final int? position;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final offline = ref.watch(offlineProvider);
    final player = ref.watch(playerProvider);
    final downloaded = offline.downloads.containsKey(track.id);
    final progress = offline.progress[track.id];
    final playable = track.playable || downloaded;
    return StreamBuilder<Track?>(
      stream: player.currentStream,
      initialData: player.current,
      builder: (context, snap) {
        final isCurrent = snap.data?.id == track.id;
        final title = Text(track.title, maxLines: 1, overflow: TextOverflow.ellipsis,
            style: TextStyle(fontWeight: FontWeight.w600, color: playable ? Colors.white : muted));
        return Pressable(
          scale: 0.98,
          onTap: onTap,
          onLongPress: () => showTrackMenu(context, ref, track, playlistId: playlistId, position: position, list: list, inLiked: inLiked),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 7, 4, 7),
            child: Row(children: [
              if (showCover)
                Stack(alignment: Alignment.center, children: [
                  Opacity(opacity: playable ? 1 : 0.45, child: Cover(track.thumbUrl, size: 50)),
                  if (isCurrent)
                    Container(
                      width: 50,
                      height: 50,
                      decoration: BoxDecoration(color: Colors.black.withValues(alpha: 0.55), borderRadius: BorderRadius.circular(radiusS)),
                      alignment: Alignment.center,
                      child: StreamBuilder<bool>(
                        stream: player.player.playingStream,
                        builder: (_, s) => EqualizerBars(playing: s.data == true, size: 18),
                      ),
                    ),
                ])
              else
                SizedBox(
                  width: 30,
                  child: isCurrent
                      ? StreamBuilder<bool>(
                          stream: player.player.playingStream,
                          builder: (_, s) => Center(child: EqualizerBars(playing: s.data == true, size: 14)),
                        )
                      : Text('${number ?? ''}', textAlign: TextAlign.center, style: const TextStyle(color: muted)),
                ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  isCurrent ? GradientMask(child: title) : title,
                  const SizedBox(height: 3),
                  Row(children: [
                    if (downloaded)
                      const Padding(padding: EdgeInsets.only(right: 4), child: Icon(Icons.download_done_rounded, size: 14, color: glowColor)),
                    if (progress != null)
                      Padding(
                        padding: const EdgeInsets.only(right: 6),
                        child: SizedBox(width: 12, height: 12, child: CircularProgressIndicator(strokeWidth: 2, value: progress == 0 ? null : progress)),
                      ),
                    if (track.explicit)
                      Container(
                        margin: const EdgeInsets.only(right: 5),
                        padding: const EdgeInsets.symmetric(horizontal: 4),
                        decoration: BoxDecoration(border: Border.all(color: muted), borderRadius: BorderRadius.circular(4)),
                        child: const Text('E', style: TextStyle(fontSize: 9, color: muted, fontWeight: FontWeight.bold)),
                      ),
                    Expanded(
                      child: Text(playable ? track.artistLine : '${track.artistLine} · ${track.statusText}',
                          maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 13)),
                    ),
                  ]),
                ]),
              ),
              IconButton(
                icon: const Icon(Icons.more_horiz_rounded, color: muted),
                onPressed: () => showTrackMenu(context, ref, track, playlistId: playlistId, position: position, list: list, inLiked: inLiked),
              ),
            ]),
          ),
        );
      },
    );
  }
}

/// [list]: the list the song is shown in, so a Jam started from it plays on through that list.
Future<void> showTrackMenu(BuildContext context, WidgetRef ref, Track track,
    {String? playlistId, int? position, List<Track>? list, bool inLiked = false}) async {
  if (track.isPreview) return;
  final player = ref.read(playerProvider);
  final offline = ref.read(offlineProvider.notifier);
  final downloaded = ref.read(offlineProvider).downloads.containsKey(track.id);
  final nav = Navigator.of(context);
  final liked = player.isLiked(track);
  final inJam = ref.read(jamProvider).jam != null;
  Widget item(IconData icon, String label, VoidCallback onTap, {Color? color}) =>
      ListTile(leading: Icon(icon, color: color), title: Text(label), onTap: onTap);
  await showModalBottomSheet<void>(
    context: context,
    useRootNavigator: true, // above the mini player and tabs
    isScrollControlled: true,
    builder: (sheet) => SafeArea(
      child: SingleChildScrollView(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          ListTile(
            leading: Cover(track.thumbUrl),
            title: Text(track.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700)),
            subtitle: Text(track.artistLine, maxLines: 1, overflow: TextOverflow.ellipsis),
          ),
          const Divider(height: 1, color: glassBorder),
          if (!track.isEpisode) ...[
            item(liked ? Icons.favorite_rounded : Icons.favorite_border_rounded, liked ? 'Remove from Liked Songs' : 'Add to Liked Songs',
                () async {
              Navigator.pop(sheet);
              await toggleLike(context, ref, track);
            }, color: liked ? accent2 : null),
            item(Icons.playlist_add_rounded, 'Add to playlist', () {
              Navigator.pop(sheet);
              addToPlaylistDialog(context, ref, [track]);
            }),
          ],
          if (inLiked && position != null && list != null && list.length > 1)
            item(Icons.swap_vert_rounded, 'Move in Liked Songs', () {
              Navigator.pop(sheet);
              nav.push(MaterialPageRoute(builder: (_) => ReorderSongsScreen.liked(tracks: list, focus: position)));
            }),
          if (playlistId != null && position != null && list != null && list.length > 1)
            item(Icons.swap_vert_rounded, 'Move in this playlist', () {
              Navigator.pop(sheet);
              nav.push(MaterialPageRoute(builder: (_) => ReorderSongsScreen(playlistId: playlistId, name: '', tracks: list, focus: position)));
            }),
          if (playlistId != null && position != null)
            item(Icons.playlist_remove_rounded, 'Remove from this playlist', () async {
              Navigator.pop(sheet);
              await ref.read(apiProvider)?.removeFromPlaylist(playlistId, position);
              ref.refreshLibrary();
            }),
          item(Icons.queue_play_next_rounded, inJam ? 'Play next in the Jam' : 'Play next', () {
            Navigator.pop(sheet);
            player.playNext(track);
          }),
          item(Icons.queue_music_rounded, inJam ? 'Add to the Jam' : 'Add to queue', () {
            Navigator.pop(sheet);
            player.addToQueue(track);
          }),
          if (!inJam && track.playable)
            item(Icons.groups_rounded, 'Start a Jam with this', () {
              Navigator.pop(sheet);
              final i = list?.indexWhere((t) => t.id == track.id) ?? -1;
              showJamSheet(context, ref, tracks: i < 0 ? [track] : list, index: i < 0 ? 0 : i);
            }, color: glowColor),
          if (!track.isEpisode)
            if (downloaded)
              item(Icons.delete_outline_rounded, 'Remove download', () {
                Navigator.pop(sheet);
                offline.remove([track.id]);
              })
            else if (track.playable)
              item(Icons.download_rounded, 'Download to this device', () {
                Navigator.pop(sheet);
                offline.download([track]);
              }),
          if (track.isEpisode && track.podcastId != null)
            item(Icons.podcasts_rounded, 'Go to podcast', () {
              Navigator.pop(sheet);
              nav.push(MaterialPageRoute(builder: (_) => PodcastScreen(track.podcastId!, title: track.album)));
            }),
          if (track.albumId != null)
            item(Icons.album_rounded, 'Go to album', () {
              Navigator.pop(sheet);
              nav.push(MaterialPageRoute(builder: (_) => TrackListScreen.album(track.albumId!, track.album ?? 'Album')));
            }),
          if (!track.isEpisode)
            for (final a in track.artists.take(3))
              item(Icons.person_rounded, 'Go to $a', () {
                Navigator.pop(sheet);
                nav.push(MaterialPageRoute(builder: (_) => ArtistScreen(a)));
              }),
        ]),
      ),
    ),
  );
}

Future<void> toggleLike(BuildContext context, WidgetRef ref, Track track) async {
  final api = ref.read(apiProvider);
  if (api == null || track.isEpisode || track.isPreview) return;
  try {
    final player = ref.read(playerProvider);
    final wasLiked = player.isLiked(track);
    player.likes.value = {...player.likes.value, track.id: !wasLiked}; // feels instant; corrected below if needed
    final liked = await api.setLiked(track.id, !wasLiked);
    player.likes.value = {...player.likes.value, track.id: liked};
    if (wasLiked && liked && context.mounted) {
      toast(context, 'It\'s liked on Spotify: unlike it there to remove it.');
    }
    ref.refreshLibrary();
  } on ApiException catch (e) {
    if (context.mounted) toast(context, e.message);
  }
}

Future<void> addToPlaylistDialog(BuildContext context, WidgetRef ref, List<Track> tracks) async {
  final api = ref.read(apiProvider);
  if (api == null) return;
  LibraryInfo lib;
  try {
    lib = await api.library();
  } on ApiException catch (e) {
    if (context.mounted) toast(context, e.message);
    return;
  }
  if (!context.mounted) return;
  final editable = lib.playlists.where((p) => p.editable).toList();
  final choice = await showModalBottomSheet<String>(
    context: context,
    useRootNavigator: true,
    builder: (sheet) => SafeArea(
      child: ListView(shrinkWrap: true, children: [
        ListTile(leading: const Icon(Icons.add_rounded), title: const Text('New playlist'), onTap: () => Navigator.pop(sheet, '+')),
        for (final p in editable)
          ListTile(
              leading: const Icon(Icons.queue_music_rounded), title: Text(p.name), subtitle: Text('${p.count} songs'), onTap: () => Navigator.pop(sheet, p.id)),
        if (editable.isEmpty)
          const Padding(
            padding: EdgeInsets.all(16),
            child: Text('Playlists synced from Spotify can only be changed in Spotify.', style: TextStyle(color: muted)),
          ),
      ]),
    ),
  );
  if (choice == null || !context.mounted) return;
  var id = choice;
  if (choice == '+') {
    final name = await askText(context, 'New playlist', 'Name');
    if (name == null || name.trim().isEmpty) return;
    id = (await api.createPlaylist(name.trim())).info['id'] as String;
  }
  await api.addToPlaylist(id, [for (final t in tracks) t.id]);
  ref.refreshLibrary();
  if (context.mounted) toast(context, tracks.length == 1 ? 'Added to playlist' : 'Added ${tracks.length} songs');
}

Future<String?> askText(BuildContext context, String title, String hint, {String initial = ''}) {
  final controller = TextEditingController(text: initial);
  return showDialog<String>(
    context: context,
    builder: (d) => AlertDialog(
      title: Text(title),
      content: TextField(controller: controller, autofocus: true, decoration: InputDecoration(hintText: hint), onSubmitted: (v) => Navigator.pop(d, v)),
      actions: [
        TextButton(onPressed: () => Navigator.pop(d), child: const Text('Cancel')),
        FilledButton(onPressed: () => Navigator.pop(d, controller.text), child: const Text('OK')),
      ],
    ),
  );
}

void openPlayer(BuildContext context) => Navigator.of(context, rootNavigator: true).push(PlayerScreen.route());

/// The floating glass capsule above the tabs: a spinning disc of the cover, the song, and a
/// thin sparkle line for progress.
class MiniPlayer extends ConsumerWidget {
  const MiniPlayer({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final c = ref.watch(playerProvider);
    final inJam = ref.watch(jamProvider).jam != null;
    return StreamBuilder<Track?>(
      stream: c.currentStream,
      initialData: c.current,
      builder: (context, snap) {
        final t = snap.data;
        return AnimatedSwitcher(
          duration: const Duration(milliseconds: 350),
          transitionBuilder: (child, a) => SizeTransition(
            sizeFactor: CurvedAnimation(parent: a, curve: Curves.easeOutCubic),
            alignment: Alignment.bottomCenter,
            child: FadeTransition(opacity: a, child: child),
          ),
          child: t == null
              ? const SizedBox(key: ValueKey('none'), width: double.infinity)
              : Padding(
                  key: const ValueKey('mini'),
                  padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
                  child: Pressable(
                    scale: 0.98,
                    onTap: () => openPlayer(context),
                    child: Glass(
                      radius: 22,
                      child: StreamBuilder<PlayerState>(
                        stream: c.player.playerStateStream,
                        initialData: c.player.playerState,
                        builder: (context, s) {
                          final state = s.data;
                          final playing = state?.playing == true;
                          final loading = state?.processingState == ProcessingState.loading || state?.processingState == ProcessingState.buffering;
                          return Column(mainAxisSize: MainAxisSize.min, children: [
                            Padding(
                              padding: const EdgeInsets.fromLTRB(8, 8, 6, 0),
                              child: Row(children: [
                                Hero(tag: 'now-art', child: SpinningDisc(url: t.thumbUrl, spinning: playing, size: 44)),
                                const SizedBox(width: 12),
                                Expanded(
                                  child: AnimatedSwitcher(
                                    duration: const Duration(milliseconds: 300),
                                    child: Column(
                                      key: ValueKey(t.id),
                                      crossAxisAlignment: CrossAxisAlignment.start,
                                      children: [
                                        Text(t.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700)),
                                        Row(children: [
                                          if (inJam) const JamTag(),
                                          Expanded(
                                            child: Text(t.isPreview ? 'Preview · ${t.artistLine}' : t.artistLine,
                                                maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 12.5)),
                                          ),
                                        ]),
                                      ],
                                    ),
                                  ),
                                ),
                                AuroraPlayButton(playing: playing, loading: loading, size: 40, onPressed: c.togglePlay),
                                IconButton(icon: const Icon(Icons.skip_next_rounded), onPressed: c.next),
                              ]),
                            ),
                            StreamBuilder<Duration>(
                              stream: c.player.positionStream,
                              builder: (context, p) => Padding(
                                padding: const EdgeInsets.symmetric(horizontal: 14),
                                child: SparkleBar(
                                  position: p.data ?? Duration.zero,
                                  duration: c.player.duration ?? t.duration,
                                  playing: playing,
                                  speed: c.player.speed,
                                  height: 12,
                                  trackHeight: 2.5,
                                  sparklesPerSecond: 4,
                                  showThumb: false,
                                ),
                              ),
                            ),
                          ]);
                        },
                      ),
                    ),
                  ),
                ),
        );
      },
    );
  }
}

/// Album art as a slowly turning disc (it pauses when the music does).
class SpinningDisc extends StatefulWidget {
  const SpinningDisc({super.key, required this.url, required this.spinning, this.size = 44});
  final String? url;
  final bool spinning;
  final double size;

  @override
  State<SpinningDisc> createState() => _SpinningDiscState();
}

class _SpinningDiscState extends State<SpinningDisc> with SingleTickerProviderStateMixin {
  late final _c = AnimationController(vsync: this, duration: const Duration(seconds: 14));

  @override
  void initState() {
    super.initState();
    if (widget.spinning) _c.repeat();
  }

  @override
  void didUpdateWidget(SpinningDisc old) {
    super.didUpdateWidget(old);
    if (widget.spinning && !_c.isAnimating && !MediaQuery.disableAnimationsOf(context)) _c.repeat();
    if (!widget.spinning && _c.isAnimating) _c.stop();
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => RotationTransition(
        turns: _c,
        child: Container(
          width: widget.size,
          height: widget.size,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            boxShadow: [BoxShadow(color: accent.withValues(alpha: 0.35), blurRadius: 12)],
          ),
          child: Stack(alignment: Alignment.center, children: [
            Cover(widget.url, size: widget.size, radius: widget.size / 2),
            Container(
              width: widget.size * 0.22,
              height: widget.size * 0.22,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: background.withValues(alpha: 0.85),
                border: Border.all(color: Colors.white24),
              ),
            ),
          ]),
        ),
      );
}
