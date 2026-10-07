import 'dart:async';
import 'dart:math';
import 'dart:ui' show ImageFilter;

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../state/data.dart';
import '../state/offline.dart';
import '../state/player.dart';
import '../state/session.dart';
import 'fx/colors.dart';
import 'fx/motion.dart';
import 'fx/sparkle.dart';
import 'theme.dart';
import 'widgets.dart';

/// A podcast's artwork, name and why it's suggested; opens the show.
class PodcastCard extends StatelessWidget {
  const PodcastCard({super.key, required this.podcast, this.showReason = false, this.size = 150});
  final PodcastSummary podcast;
  final bool showReason;
  final double size;

  @override
  Widget build(BuildContext context) => Pressable(
        onTap: () => push(context, PodcastScreen(podcast.id, title: podcast.title)),
        child: SizedBox(
          width: size,
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Cover(podcast.artworkUrl, size: size, radius: radiusL, icon: Icons.podcasts_rounded),
            const SizedBox(height: 8),
            Text(podcast.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700)),
            Text(
              showReason && podcast.reason != null ? podcast.reason! : (podcast.author ?? podcast.genre ?? ''),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(color: showReason && podcast.reason != null ? glowColor : muted, fontSize: 12),
            ),
          ]),
        ),
      );
}

class PodcastScreen extends ConsumerStatefulWidget {
  const PodcastScreen(this.id, {super.key, this.title});
  final String id;
  final String? title;

  @override
  ConsumerState<PodcastScreen> createState() => _PodcastScreenState();
}

class _PodcastScreenState extends ConsumerState<PodcastScreen> {
  bool? _following; // while a follow/unfollow is in flight
  String? _downloads; // likewise for the download setting
  bool _expanded = false;

  Future<void> _setDownloads(String keep) async {
    final api = ref.read(apiProvider);
    if (api == null) return;
    final before = _downloads;
    setState(() => _downloads = keep);
    try {
      await api.setPodcastDownloads(widget.id, keep);
      ref.invalidate(podcastProvider(widget.id));
      ref.read(offlineProvider.notifier).syncEpisodesSoon();
      if (mounted && keep != 'off') toast(context, 'New episodes will download to your server and this phone');
    } on ApiException catch (e) {
      setState(() => _downloads = before);
      if (mounted) toast(context, e.message);
    }
  }

  Future<void> _toggleFollow(PodcastDetail d) async {
    final api = ref.read(apiProvider);
    if (api == null) return;
    final want = !(_following ?? d.summary.following);
    setState(() => _following = want);
    try {
      final now = await api.setFollowing(widget.id, want);
      if (!want && now && mounted) toast(context, 'It\'s saved on Spotify: remove it there to unfollow.');
      setState(() => _following = now);
      ref.refreshPodcasts();
    } on ApiException catch (e) {
      setState(() => _following = !want);
      if (mounted) toast(context, e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final value = ref.watch(podcastProvider(widget.id));
    final d = value.value;
    final art = d?.summary.artworkUrl;
    final tint = art == null ? null : ref.watch(artColorProvider(art)).value;
    final following = _following ?? d?.summary.following ?? false;
    return Scaffold(
      body: Stack(children: [
        RefreshIndicator(
          color: accent,
          backgroundColor: surface,
          onRefresh: () async => ref.invalidate(podcastProvider(widget.id)),
          child: CustomScrollView(slivers: [
            SliverToBoxAdapter(
              child: Stack(children: [
                if (art != null)
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
                          child: CachedNetworkImage(imageUrl: art, fit: BoxFit.cover, memCacheWidth: 200, errorWidget: (_, _, _) => const SizedBox()),
                        ),
                      ),
                    ),
                  ),
                SafeArea(
                  bottom: false,
                  child: Padding(
                    padding: const EdgeInsets.fromLTRB(24, 60, 24, 8),
                    child: Column(children: [
                      FadeSlideIn(
                        child: Container(
                          decoration: BoxDecoration(
                            borderRadius: BorderRadius.circular(radiusL),
                            boxShadow: [BoxShadow(color: (tint ?? accent).withValues(alpha: 0.45), blurRadius: 50)],
                          ),
                          child: Cover(art, size: 190, radius: radiusL, icon: Icons.podcasts_rounded),
                        ),
                      ),
                      const SizedBox(height: 18),
                      Text(d?.summary.title ?? widget.title ?? '', textAlign: TextAlign.center, style: Theme.of(context).textTheme.headlineMedium),
                      if (d?.summary.author != null)
                        Padding(
                          padding: const EdgeInsets.only(top: 4),
                          child: Text(d!.summary.author!, textAlign: TextAlign.center, style: const TextStyle(color: muted)),
                        ),
                      const SizedBox(height: 16),
                      if (d != null) _FollowButton(following: following, onPressed: () => _toggleFollow(d)),
                      if (d != null) ...[
                        const SizedBox(height: 16),
                        _DownloadSetting(value: _downloads ?? d.downloads, onChanged: _setDownloads),
                      ],
                      if (d?.description != null) ...[
                        const SizedBox(height: 14),
                        GestureDetector(
                          onTap: () => setState(() => _expanded = !_expanded),
                          child: AnimatedSize(
                            duration: const Duration(milliseconds: 300),
                            curve: Curves.easeOutCubic,
                            alignment: Alignment.topCenter,
                            child: Text(d!.description!, maxLines: _expanded ? null : 3, overflow: _expanded ? null : TextOverflow.ellipsis,
                                textAlign: TextAlign.center, style: const TextStyle(color: Colors.white70, fontSize: 13.5, height: 1.4)),
                          ),
                        ),
                      ],
                      if (d?.stale == true)
                        const Padding(
                          padding: EdgeInsets.only(top: 10),
                          child: Text('Couldn\'t check for new episodes just now.', style: TextStyle(color: muted, fontSize: 12)),
                        ),
                    ]),
                  ),
                ),
              ]),
            ),
            ...value.when(
              loading: () => [const SliverToBoxAdapter(child: ListSkeleton(rows: 6))],
              error: (e, _) => [SliverToBoxAdapter(child: ErrorView(e, onRetry: () => ref.invalidate(podcastProvider(widget.id))))],
              data: (d) => [
                SliverToBoxAdapter(child: SectionTitle('Episodes', subtitle: '${d.episodes.length} available')),
                SliverList.builder(
                  itemCount: d.episodes.length,
                  itemBuilder: (context, i) {
                    final tile = EpisodeTile(episode: d.episodes[i]);
                    return i < 12 ? FadeSlideIn(index: i, child: tile) : tile;
                  },
                ),
              ],
            ),
            const SliverToBoxAdapter(child: SizedBox(height: 190)),
          ]),
        ),
        Positioned(
          left: 16,
          top: MediaQuery.paddingOf(context).top + 8,
          child: GlassIconButton(icon: Icons.arrow_back_rounded, tooltip: 'Back', onPressed: () => Navigator.maybePop(context)),
        ),
      ]),
    );
  }
}

/// How this show's new episodes download. They never stay forever: either until you've
/// played them, or for a number of days.
class _DownloadSetting extends StatelessWidget {
  const _DownloadSetting({required this.value, required this.onChanged});
  final String value;
  final ValueChanged<String> onChanged;

  static const _choices = [('off', 'Off'), ('played', 'Until played'), ('7', '7 days'), ('30', '30 days')];

  String get _explain => switch (value) {
        'off' => 'Episodes stream when you play them.',
        'played' => 'The newest episodes download to your server and this phone, and each one is deleted once you\'ve finished it.',
        _ => 'The newest episodes download to your server and this phone, and are deleted after $value days.',
      };

  @override
  Widget build(BuildContext context) => Glass(
        blur: false,
        radius: radiusM,
        padding: const EdgeInsets.fromLTRB(12, 12, 12, 10),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Row(children: [
            GradientMask(child: Icon(Icons.download_for_offline_rounded, size: 18)),
            SizedBox(width: 8),
            Text('Download new episodes', style: TextStyle(fontWeight: FontWeight.w700)),
          ]),
          const SizedBox(height: 10),
          Row(children: [
            for (final (keep, label) in _choices)
              Expanded(
                child: Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 2),
                  child: Pressable(
                    onTap: () => onChanged(keep),
                    child: AnimatedContainer(
                      duration: const Duration(milliseconds: 280),
                      padding: const EdgeInsets.symmetric(vertical: 8),
                      alignment: Alignment.center,
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(14),
                        gradient: value == keep ? auroraSoft : null,
                        border: Border.all(color: value == keep ? Colors.transparent : glassBorder),
                      ),
                      child: Text(label, maxLines: 1, style: TextStyle(fontSize: 12.5, fontWeight: FontWeight.w700, color: value == keep ? Colors.white : Colors.white70)),
                    ),
                  ),
                ),
              ),
          ]),
          const SizedBox(height: 8),
          AnimatedSwitcher(
            duration: const Duration(milliseconds: 250),
            child: Text(_explain, key: ValueKey(value), style: const TextStyle(color: muted, fontSize: 12.5, height: 1.35)),
          ),
        ]),
      );
}

class _FollowButton extends StatelessWidget {
  const _FollowButton({required this.following, required this.onPressed});
  final bool following;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) => Pressable(
        onTap: onPressed,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 350),
          curve: Curves.easeOutCubic,
          padding: const EdgeInsets.symmetric(horizontal: 22, vertical: 11),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(24),
            gradient: following ? null : aurora,
            border: Border.all(color: following ? glassBorder : Colors.transparent),
            color: following ? glassFill : null,
            boxShadow: following ? const [] : [BoxShadow(color: accent2.withValues(alpha: 0.35), blurRadius: 18)],
          ),
          child: Row(mainAxisSize: MainAxisSize.min, children: [
            AnimatedSwitcher(
              duration: const Duration(milliseconds: 300),
              transitionBuilder: (c, a) => ScaleTransition(scale: a, child: c),
              child: Icon(following ? Icons.check_rounded : Icons.add_rounded, key: ValueKey(following), size: 20),
            ),
            const SizedBox(width: 6),
            Text(following ? 'Following' : 'Follow', style: const TextStyle(fontWeight: FontWeight.w800)),
          ]),
        ),
      );
}

/// One episode: date and length, how far you got, and a play button that resumes.
class EpisodeTile extends ConsumerWidget {
  const EpisodeTile({super.key, required this.episode, this.showPodcast = false});
  final Episode episode;
  final bool showPodcast;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final player = ref.watch(playerProvider);
    final offline = ref.watch(offlineProvider);
    final e = episode;
    final onPhone = offline.episodes.containsKey(e.id);
    final kept = onPhone ? offline.episodes[e.id]!.keepLabel ?? e.keepLabel : e.keepLabel;
    return StreamBuilder<Track?>(
      stream: player.currentStream,
      initialData: player.current,
      builder: (context, snap) {
        final isCurrent = snap.data?.id == e.id;
        final meta = [
          formatDate(e.published),
          if (e.duration > Duration.zero)
            e.completed
                ? 'Played'
                : e.progress > Duration.zero
                    ? '${formatLength(e.duration - e.progress)} left'
                    : formatLength(e.duration),
          if (kept != null) onPhone ? 'On this phone, $kept' : 'Downloading, $kept',
        ].where((s) => s.isNotEmpty).join(' · ');
        final title = Text(e.title, maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700, height: 1.25));
        return Pressable(
          scale: 0.98,
          onTap: () => isCurrent ? player.togglePlay() : player.play([e.toTrack()]),
          onLongPress: e.description == null ? null : () => _notes(context),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 10, 16, 10),
            child: Row(children: [
              Cover(e.imageUrl, size: 64, radius: radiusS, icon: Icons.podcasts_rounded),
              const SizedBox(width: 14),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  if (showPodcast) Text(e.podcastTitle, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: glowColor, fontSize: 12)),
                  isCurrent ? GradientMask(child: title) : Opacity(opacity: e.completed ? 0.6 : 1, child: title),
                  const SizedBox(height: 4),
                  Text(meta, style: const TextStyle(color: muted, fontSize: 12.5)),
                ]),
              ),
              _EpisodeDownload(episode: e, onPhone: onPhone, progress: offline.progress[e.id]),
              StreamBuilder<bool>(
                stream: player.player.playingStream,
                builder: (_, s) => _ProgressPlay(fraction: e.fraction, playing: isCurrent && s.data == true, done: e.completed),
              ),
            ]),
          ),
        );
      },
    );
  }

  void _notes(BuildContext context) => showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        builder: (sheet) => DraggableScrollableSheet(
          expand: false,
          initialChildSize: 0.6,
          builder: (context, scroll) => ListView(controller: scroll, padding: const EdgeInsets.fromLTRB(24, 0, 24, 32), children: [
            Text(episode.title, style: Theme.of(context).textTheme.titleLarge),
            const SizedBox(height: 6),
            Text('${episode.podcastTitle} · ${formatDate(episode.published)}', style: const TextStyle(color: muted)),
            const SizedBox(height: 16),
            Text(episode.description ?? '', style: const TextStyle(height: 1.5)),
          ]),
        ),
      );
}

/// Keep one episode downloaded (until played, or for some days), or let it go.
class _EpisodeDownload extends ConsumerWidget {
  const _EpisodeDownload({required this.episode, required this.onPhone, required this.progress});
  final Episode episode;
  final bool onPhone;
  final double? progress;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final kept = episode.keep != null || onPhone;
    final Widget icon = progress != null
        ? SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2.2, value: progress == 0 ? null : progress))
        : onPhone
            ? const GradientMask(child: Icon(Icons.download_done_rounded))
            : kept
                ? const Icon(Icons.downloading_rounded, color: glowColor)
                : const Icon(Icons.download_rounded, color: muted);
    return IconButton(
      tooltip: kept ? 'Downloaded' : 'Download',
      icon: icon,
      onPressed: () => _choose(context, ref, kept),
    );
  }

  Future<void> _choose(BuildContext context, WidgetRef ref, bool kept) async {
    final choice = await showModalBottomSheet<String>(
      context: context,
      builder: (sheet) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(24, 0, 24, 8),
            child: Text(kept ? 'Keep this episode' : 'Download this episode', style: Theme.of(context).textTheme.titleLarge),
          ),
          for (final (keep, label, icon) in const [
            ('played', 'Until I\'ve played it', Icons.play_circle_outline_rounded),
            ('3', 'For 3 days', Icons.timer_outlined),
            ('7', 'For a week', Icons.date_range_rounded),
            ('30', 'For 30 days', Icons.event_rounded),
          ])
            ListTile(
              leading: Icon(icon),
              title: Text(label),
              trailing: episode.keep == keep ? const Icon(Icons.check_rounded, color: glowColor) : null,
              onTap: () => Navigator.pop(sheet, keep),
            ),
          if (kept)
            ListTile(
              leading: const Icon(Icons.delete_outline_rounded, color: Color(0xFFFDA4AF)),
              title: const Text('Remove download', style: TextStyle(color: Color(0xFFFDA4AF))),
              onTap: () => Navigator.pop(sheet, 'remove'),
            ),
        ]),
      ),
    );
    if (choice == null) return;
    final api = ref.read(apiProvider);
    if (api == null) return;
    final offline = ref.read(offlineProvider.notifier);
    try {
      if (choice == 'remove') {
        await api.dropEpisode(episode.id);
        await offline.syncEpisodes();
      } else {
        await api.keepEpisode(episode.id, choice);
        offline.syncEpisodesSoon();
        if (context.mounted) toast(context, choice == 'played' ? 'Downloading: it goes once you\'ve played it' : 'Downloading for $choice days');
      }
      ref.invalidate(podcastProvider(episode.podcastId));
      ref.invalidate(podcastsProvider);
    } on ApiException catch (e) {
      if (context.mounted) toast(context, e.message);
    }
  }
}

/// A round play button ringed by how much of the episode you've heard.
class _ProgressPlay extends StatelessWidget {
  const _ProgressPlay({required this.fraction, required this.playing, required this.done});
  final double fraction;
  final bool playing;
  final bool done;

  @override
  Widget build(BuildContext context) => SizedBox(
        width: 44,
        height: 44,
        child: CustomPaint(
          painter: _RingPainter(done ? 1 : fraction),
          child: Center(
            child: done
                ? const Icon(Icons.check_rounded, size: 20, color: muted)
                : playing
                    ? const EqualizerBars(playing: true, size: 16)
                    : const Icon(Icons.play_arrow_rounded, size: 24),
          ),
        ),
      );
}

class _RingPainter extends CustomPainter {
  _RingPainter(this.fraction);
  final double fraction;

  @override
  void paint(Canvas canvas, Size size) {
    final rect = (Offset.zero & size).deflate(2);
    canvas.drawCircle(rect.center, rect.width / 2, Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2
      ..color = Colors.white24);
    if (fraction > 0) {
      canvas.drawArc(rect, -pi / 2, 2 * pi * fraction, false, Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2.5
        ..strokeCap = StrokeCap.round
        ..shader = const SweepGradient(colors: [accent, accent2, accent3, accent]).createShader(rect));
    }
  }

  @override
  bool shouldRepaint(_RingPainter old) => old.fraction != fraction;
}

/// Your shows, what's new, what you were partway through, and podcasts picked for you.
class PodcastsScreen extends ConsumerStatefulWidget {
  const PodcastsScreen({super.key});
  @override
  ConsumerState<PodcastsScreen> createState() => _PodcastsScreenState();
}

class _PodcastsScreenState extends ConsumerState<PodcastsScreen> {
  final _controller = TextEditingController();
  Timer? _debounce;
  String _query = '';

  @override
  void dispose() {
    _debounce?.cancel();
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final value = ref.watch(podcastsProvider);
    final onPhone = ref.watch(offlineProvider).episodes.values.toList();
    return Scaffold(
      appBar: AppBar(
        leading: Padding(padding: const EdgeInsets.all(6), child: GlassIconButton(icon: Icons.arrow_back_rounded, onPressed: () => Navigator.maybePop(context))),
        title: const Text('Podcasts'),
      ),
      body: RefreshIndicator(
        color: accent,
        backgroundColor: surface,
        onRefresh: () async => ref.invalidate(podcastsProvider),
        child: ListView(padding: const EdgeInsets.only(bottom: 190), children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 4),
            child: Glass(
              radius: radiusL,
              child: TextField(
                controller: _controller,
                onChanged: (v) {
                  _debounce?.cancel();
                  _debounce = Timer(const Duration(milliseconds: 400), () => setState(() => _query = v.trim()));
                },
                decoration: const InputDecoration(
                  hintText: 'Find a podcast',
                  prefixIcon: Icon(Icons.search_rounded, color: muted),
                  filled: false,
                  border: InputBorder.none,
                  enabledBorder: InputBorder.none,
                  focusedBorder: InputBorder.none,
                  contentPadding: EdgeInsets.symmetric(vertical: 15),
                ),
              ),
            ),
          ),
          if (_query.isNotEmpty)
            ...ref.watch(podcastSearchProvider(_query)).when(
                  loading: () => [const ListSkeleton(rows: 4)],
                  error: (e, _) => [ErrorView(e)],
                  data: (list) => [
                    if (list.isEmpty) const Padding(padding: EdgeInsets.all(20), child: Text('No podcasts found.', style: TextStyle(color: muted))),
                    for (final p in list)
                      ListTile(
                        contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 4),
                        leading: Cover(p.artworkUrl, size: 56, icon: Icons.podcasts_rounded),
                        title: Text(p.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700)),
                        subtitle: Text([p.author, p.genre].whereType<String>().join(' · '), maxLines: 1, overflow: TextOverflow.ellipsis),
                        trailing: p.following ? const Icon(Icons.check_circle_rounded, color: glowColor) : null,
                        onTap: () => push(context, PodcastScreen(p.id, title: p.title)),
                      ),
                  ],
                )
          else
            ...value.when(
              loading: () => [const ListSkeleton()],
              error: (e, _) => [ErrorView(e, onRetry: () => ref.invalidate(podcastsProvider))],
              data: (s) => [
                if (onPhone.isNotEmpty) ...[
                  const SectionTitle('On this phone', subtitle: 'Removed when played or when their days run out'),
                  for (final e in onPhone) EpisodeTile(episode: e, showPodcast: true),
                ],
                if (s.continueListening.isNotEmpty) ...[
                  const SectionTitle('Continue listening'),
                  for (final e in s.continueListening) EpisodeTile(episode: e, showPodcast: true),
                ],
                if (s.newEpisodes.isNotEmpty) ...[
                  const SectionTitle('New episodes'),
                  for (final e in s.newEpisodes.take(10)) EpisodeTile(episode: e, showPodcast: true),
                ],
                if (s.following.isNotEmpty) ...[
                  const SectionTitle('Your podcasts'),
                  SizedBox(
                    height: 206,
                    child: ListView.separated(
                      scrollDirection: Axis.horizontal,
                      padding: const EdgeInsets.symmetric(horizontal: 20),
                      itemCount: s.following.length,
                      separatorBuilder: (_, _) => const SizedBox(width: 14),
                      itemBuilder: (context, i) => PodcastCard(podcast: s.following[i]),
                    ),
                  ),
                ],
                if (s.following.isEmpty)
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 16, 16, 0),
                    child: Glass(
                      blur: false,
                      child: SizedBox(
                        height: 120,
                        child: Stack(children: [
                          const Positioned.fill(child: AmbientSparkles(perSecond: 4)),
                          const Padding(
                            padding: EdgeInsets.all(20),
                            child: Text(
                              'Follow a show to see its new episodes here. Podcasts you save on Spotify appear automatically '
                              'when they have a public feed.',
                              style: TextStyle(color: Colors.white70, height: 1.4),
                            ),
                          ),
                        ]),
                      ),
                    ),
                  ),
                if (s.recommended.isNotEmpty) ...[
                  const SectionTitle('Podcasts for you'),
                  SizedBox(
                    height: 226,
                    child: ListView.separated(
                      scrollDirection: Axis.horizontal,
                      padding: const EdgeInsets.symmetric(horizontal: 20),
                      itemCount: s.recommended.length,
                      separatorBuilder: (_, _) => const SizedBox(width: 14),
                      itemBuilder: (context, i) => PodcastCard(podcast: s.recommended[i], showReason: true),
                    ),
                  ),
                ],
              ],
            ),
        ]),
      ),
    );
  }
}
