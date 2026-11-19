import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/models.dart';
import '../state/data.dart';
import 'fx/motion.dart';
import 'fx/sparkle.dart';
import 'library_screen.dart';
import 'theme.dart';
import 'tracklist_screen.dart';
import 'widgets.dart';

/// Someone on the server as a round badge: their first letter on their own colours.
class PersonAvatar extends StatelessWidget {
  const PersonAvatar(this.name, {super.key, this.size = 60, this.glow = false});
  final String name;
  final double size;
  final bool glow;

  @override
  Widget build(BuildContext context) {
    final colors = genreColors(name);
    final letter = name.trim().isEmpty ? '?' : name.trim().characters.first.toUpperCase();
    return Container(
      width: size,
      height: size,
      alignment: Alignment.center,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        gradient: LinearGradient(colors: colors, begin: Alignment.topLeft, end: Alignment.bottomRight),
        boxShadow: glow ? [BoxShadow(color: colors.first.withValues(alpha: 0.5), blurRadius: 50, spreadRadius: 2)] : null,
      ),
      child: Text(letter, style: TextStyle(fontSize: size * 0.42, fontWeight: FontWeight.w800, color: Colors.white)),
    );
  }
}

/// Someone else's music: their Liked Songs and playlists, to look through and play (not to change).
class PersonScreen extends ConsumerWidget {
  const PersonScreen(this.person, {super.key});
  final Person person;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final page = ref.watch(personProvider(person.id));
    var n = 0;
    Widget row(Widget w) => FadeSlideIn(index: n++, child: w);
    return Scaffold(
      body: Stack(children: [
        const Positioned.fill(child: AmbientSparkles(perSecond: 4, colors: sparkleColors)),
        RefreshIndicator(
          color: accent,
          backgroundColor: surface,
          onRefresh: () async => ref.invalidate(personProvider(person.id)),
          child: ListView(padding: const EdgeInsets.only(bottom: 190), children: [
            SafeArea(
              bottom: false,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
                child: Align(
                  alignment: Alignment.centerLeft,
                  child: GlassIconButton(icon: Icons.arrow_back_rounded, tooltip: 'Back', onPressed: () => Navigator.maybePop(context)),
                ),
              ),
            ),
            const SizedBox(height: 8),
            Center(child: FadeSlideIn(child: PersonAvatar(person.name, size: 150, glow: true))),
            const SizedBox(height: 20),
            Text(person.name, textAlign: TextAlign.center, style: Theme.of(context).textTheme.headlineMedium),
            const SizedBox(height: 4),
            Text(
              page.whenOrNull(data: (p) => _counts(p.likedCount, p.playlists.length)) ?? _counts(person.likedCount, person.playlistCount),
              textAlign: TextAlign.center,
              style: const TextStyle(color: muted, fontSize: 13),
            ),
            const SizedBox(height: 18),
            ...page.when(
              loading: () => [const ListSkeleton(rows: 4)],
              error: (e, _) => [ErrorView(e, onRetry: () => ref.invalidate(personProvider(person.id)))],
              data: (p) => [
                row(LibraryRow(
                  leading: const LikedCover(size: 60),
                  title: 'Liked Songs',
                  subtitle: '${p.likedCount} song${p.likedCount == 1 ? '' : 's'}',
                  onTap: () => push(context, TrackListScreen.theirLiked(p.id, p.name)),
                )),
                if (p.playlists.isNotEmpty) const SectionTitle('Playlists'),
                for (final pl in p.playlists)
                  row(LibraryRow(
                    leading: Cover(pl.imageUrl, size: 60, icon: Icons.queue_music_rounded),
                    title: pl.name,
                    subtitle: 'Playlist · ${pl.count} song${pl.count == 1 ? '' : 's'}',
                    onTap: () => push(context, TrackListScreen.playlist(pl.id, pl.name)),
                  )),
                if (p.playlists.isEmpty)
                  Padding(
                    padding: const EdgeInsets.fromLTRB(32, 24, 32, 0),
                    child: Text('${p.name} hasn\'t made any playlists yet.', textAlign: TextAlign.center, style: const TextStyle(color: muted)),
                  ),
              ],
            ),
          ]),
        ),
      ]),
    );
  }

  static String _counts(int liked, int playlists) =>
      '$liked liked song${liked == 1 ? '' : 's'} · $playlists playlist${playlists == 1 ? '' : 's'}';
}
