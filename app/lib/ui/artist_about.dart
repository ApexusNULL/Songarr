import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/models.dart';
import '../state/data.dart';
import 'fx/motion.dart';
import 'theme.dart';
import 'widgets.dart';

/// "About": a short bio, quick facts and the artist's story as a timeline.
class ArtistAboutSection extends ConsumerWidget {
  const ArtistAboutSection(this.name, {super.key});
  final String name;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final value = ref.watch(artistAboutProvider(name));
    return value.when(
      loading: () => const _Looking(),
      error: (_, _) => const SizedBox.shrink(),
      data: (about) => about == null || about.summary.isEmpty ? const SizedBox.shrink() : _About(about),
    );
  }
}

class _Looking extends StatelessWidget {
  const _Looking();

  @override
  Widget build(BuildContext context) => const Padding(
        padding: EdgeInsets.fromLTRB(16, 24, 16, 0),
        child: Glass(
          blur: false,
          padding: EdgeInsets.all(18),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text('Looking up their story…', style: TextStyle(color: muted)),
            SizedBox(height: 12),
            ShimmerBox(height: 12, radius: 6),
            SizedBox(height: 8),
            ShimmerBox(height: 12, radius: 6),
            SizedBox(height: 8),
            ShimmerBox(width: 180, height: 12, radius: 6),
          ]),
        ),
      );
}

class _About extends StatefulWidget {
  const _About(this.about);
  final ArtistAbout about;

  @override
  State<_About> createState() => _AboutState();
}

class _AboutState extends State<_About> {
  bool _more = false;
  bool _fullStory = false;

  @override
  Widget build(BuildContext context) {
    final a = widget.about;
    final story = a.history;
    final shown = _fullStory ? story : story.take(3).toList();
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      SectionTitle('About', subtitle: a.description),
      Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: FadeSlideIn(
          child: Glass(
            blur: false,
            padding: const EdgeInsets.fromLTRB(18, 16, 18, 14),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              GestureDetector(
                onTap: () => setState(() => _more = !_more),
                child: AnimatedSize(
                  duration: const Duration(milliseconds: 300),
                  curve: Curves.easeOutCubic,
                  alignment: Alignment.topCenter,
                  child: Text(a.summary, maxLines: _more ? null : 5, overflow: _more ? null : TextOverflow.fade,
                      style: const TextStyle(height: 1.45, fontSize: 14.5, color: Colors.white)),
                ),
              ),
              if (a.summary.length > 260)
                TextButton(
                  style: TextButton.styleFrom(padding: EdgeInsets.zero, minimumSize: const Size(0, 34)),
                  onPressed: () => setState(() => _more = !_more),
                  child: Text(_more ? 'Less' : 'More'),
                ),
              const SizedBox(height: 6),
              Wrap(spacing: 8, runSpacing: 8, children: [
                if (a.origin != null) _Fact(icon: a.isGroup ? Icons.groups_rounded : Icons.cake_rounded, text: a.origin!),
                for (final g in a.genres) _Fact(icon: Icons.graphic_eq_rounded, text: g),
                if (a.labels.isNotEmpty) _Fact(icon: Icons.album_rounded, text: a.labels.join(', ')),
              ]),
              if (a.members.isNotEmpty) ...[
                const SizedBox(height: 12),
                Text('Members', style: TextStyle(color: muted.withValues(alpha: 0.9), fontSize: 12, letterSpacing: 1.2, fontWeight: FontWeight.w700)),
                const SizedBox(height: 4),
                Text(a.members.join(' · '), style: const TextStyle(fontSize: 13.5)),
              ],
            ]),
          ),
        ),
      ),
      if (story.isNotEmpty) ...[
        const SectionTitle('Their story'),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 20),
          child: AnimatedSize(
            duration: const Duration(milliseconds: 400),
            curve: Curves.easeOutCubic,
            alignment: Alignment.topCenter,
            child: Column(children: [
              for (var i = 0; i < shown.length; i++)
                FadeSlideIn(index: i, child: _Chapter(heading: shown[i].heading, text: shown[i].text, last: i == shown.length - 1)),
            ]),
          ),
        ),
        if (story.length > 3)
          Padding(
            padding: const EdgeInsets.fromLTRB(44, 0, 20, 0),
            child: TextButton(
              onPressed: () => setState(() => _fullStory = !_fullStory),
              child: Text(_fullStory ? 'Show less' : 'Show the full story (${story.length} chapters)'),
            ),
          ),
      ],
      Padding(
        padding: const EdgeInsets.fromLTRB(20, 8, 20, 0),
        child: Text('From ${a.source}${a.license == null ? '' : ' · ${a.license}'}', style: const TextStyle(color: muted, fontSize: 11.5)),
      ),
    ]);
  }
}

class _Fact extends StatelessWidget {
  const _Fact({required this.icon, required this.text});
  final IconData icon;
  final String text;

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
        decoration: BoxDecoration(borderRadius: BorderRadius.circular(14), color: glassFill, border: Border.all(color: glassBorder)),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          GradientMask(child: Icon(icon, size: 14)),
          const SizedBox(width: 6),
          Flexible(child: Text(text, style: const TextStyle(fontSize: 12.5))),
        ]),
      );
}

/// One step of the story: a glowing dot on a line, the years and title, and what happened.
class _Chapter extends StatelessWidget {
  const _Chapter({required this.heading, required this.text, required this.last});
  final String heading;
  final String text;
  final bool last;

  @override
  Widget build(BuildContext context) {
    // "2009–2011: Selfish Machines" -> years in the gradient, title in white.
    final m = RegExp(r'^(\d{4}\s*[–-]\s*(?:\d{4}|present))\s*:\s*(.+)$').firstMatch(heading);
    return IntrinsicHeight(
      child: Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        SizedBox(
          width: 24,
          child: Column(children: [
            const SizedBox(height: 4),
            Container(
              width: 12,
              height: 12,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                gradient: aurora,
                boxShadow: [BoxShadow(color: accent2.withValues(alpha: 0.6), blurRadius: 10)],
              ),
            ),
            if (!last)
              Expanded(
                child: Container(
                  width: 2,
                  margin: const EdgeInsets.symmetric(vertical: 4),
                  decoration: BoxDecoration(
                    gradient: LinearGradient(colors: [accent.withValues(alpha: 0.6), accent2.withValues(alpha: 0.15)],
                        begin: Alignment.topCenter, end: Alignment.bottomCenter),
                  ),
                ),
              ),
          ]),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Padding(
            padding: const EdgeInsets.only(bottom: 22),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              if (m != null) ...[
                GradientMask(child: Text(m.group(1)!, style: const TextStyle(fontWeight: FontWeight.w900, fontSize: 13, letterSpacing: 0.5))),
                Text(m.group(2)!, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 15.5)),
              ] else if (heading.isNotEmpty)
                Text(heading, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 15.5)),
              const SizedBox(height: 6),
              Text(text, style: const TextStyle(color: Colors.white70, height: 1.45, fontSize: 13.5)),
            ]),
          ),
        ),
      ]),
    );
  }
}

/// The search's "Top result": the artist, with the start of their bio.
class TopArtistCard extends ConsumerWidget {
  const TopArtistCard({super.key, required this.artist, required this.onTap});
  final ArtistRec artist;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final value = ref.watch(artistAboutProvider(artist.name));
    final about = value.value;
    final line = [
      about?.description ?? 'Artist',
      if (artist.songsOnServer > 0) '${artist.songsOnServer} song${artist.songsOnServer == 1 ? '' : 's'} on your server',
    ].join(' · ');
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
      child: Pressable(
        onTap: onTap,
        child: Glass(
          blur: false,
          padding: const EdgeInsets.all(14),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            ArtistAvatar(artist.thumbUrl ?? about?.imageUrl, size: 84),
            const SizedBox(width: 14),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                const Text('TOP RESULT', style: TextStyle(fontSize: 10.5, letterSpacing: 2, color: muted, fontWeight: FontWeight.w800)),
                const SizedBox(height: 2),
                Text(artist.name, maxLines: 1, overflow: TextOverflow.ellipsis, style: Theme.of(context).textTheme.titleLarge),
                Text(line, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: glowColor, fontSize: 12.5)),
                const SizedBox(height: 6),
                AnimatedSwitcher(
                  duration: const Duration(milliseconds: 300),
                  child: about == null
                      ? (value.isLoading ? const ShimmerBox(key: ValueKey('wait'), height: 10, radius: 5) : const SizedBox.shrink())
                      : Text(about.summary, key: const ValueKey('bio'), maxLines: 3, overflow: TextOverflow.ellipsis,
                          style: const TextStyle(color: Colors.white70, fontSize: 13, height: 1.35)),
                ),
              ]),
            ),
          ]),
        ),
      ),
    );
  }
}
