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

/// A song downloaded as the wrong version (a cover, a live take, another band): pick the right one
/// from the uploads the server found, search YouTube again, or paste a link. The server downloads
/// it in place of the old one, for everyone.
class ReplaceSongScreen extends ConsumerStatefulWidget {
  const ReplaceSongScreen(this.track, {super.key});
  final Track track;

  @override
  ConsumerState<ReplaceSongScreen> createState() => _ReplaceSongScreenState();
}

class _ReplaceSongScreenState extends ConsumerState<ReplaceSongScreen> {
  SongVersions? _versions;
  String? _error;
  bool _searching = false;
  bool _saving = false;
  String? _picked; // a youtube id from the list
  final _link = TextEditingController();

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _link.dispose();
    super.dispose();
  }

  Future<void> _load({bool search = false}) async {
    final api = ref.read(apiProvider);
    if (api == null) return;
    setState(() {
      _searching = search;
      _error = null;
    });
    try {
      final v = await api.songVersions(widget.track.id, search: search);
      if (mounted) setState(() => _versions = v);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _searching = false);
    }
  }

  String? get _choice => _link.text.trim().isNotEmpty ? _link.text.trim() : _picked;

  Future<void> _replace() async {
    final choice = _choice, api = ref.read(apiProvider);
    if (choice == null || api == null) return;
    final ok = await showDialog<bool>(
      context: context,
      builder: (d) => AlertDialog(
        title: const Text('Replace this song?'),
        content: Text('"${widget.track.title}" is downloaded again from the version you picked, for everyone on the server. '
            'It takes a minute or so; until then it plays the version it has.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(d, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(d, true), child: const Text('Replace')),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    setState(() => _saving = true);
    try {
      await api.replaceSong(widget.track.id, choice);
      ref.refreshLibrary();
      if (!mounted) return;
      toast(context, 'Replacing "${widget.track.title}". It\'ll play the new version in a minute or so.');
      Navigator.pop(context);
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message);
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final v = _versions;
    final current = v?.versions.where((x) => x.current).firstOrNull;
    final others = v?.versions.where((x) => !x.current).toList() ?? const <SongVersion>[];
    return Scaffold(
      appBar: AppBar(
        leading: Padding(
            padding: const EdgeInsets.all(6),
            child: GlassIconButton(icon: Icons.arrow_back_rounded, onPressed: () => Navigator.maybePop(context))),
        title: const Text('Replace this song'),
      ),
      body: ListView(padding: const EdgeInsets.fromLTRB(16, 4, 16, 190), children: [
        ListTile(
          contentPadding: EdgeInsets.zero,
          leading: Cover(widget.track.thumbUrl, size: 52),
          title: Text(widget.track.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w800)),
          subtitle: Text([widget.track.artistLine, if (v?.expected != null) 'should be ${_clock(v!.expected!)} long'].join(' · '),
              maxLines: 1, overflow: TextOverflow.ellipsis),
        ),
        const Padding(
          padding: EdgeInsets.only(bottom: 8),
          child: Text('Downloaded the wrong version, like a cover or a live take? Pick the right one and it replaces this song for everyone.',
              style: TextStyle(color: muted, height: 1.35)),
        ),
        if (v == null && _error == null) const Padding(padding: EdgeInsets.all(40), child: Center(child: CircularProgressIndicator(color: accent))),
        if (_error != null) Padding(padding: const EdgeInsets.symmetric(vertical: 12), child: Text(_error!, style: const TextStyle(color: Color(0xFFFDA4AF)))),
        if (v != null) ...[
          const _Heading('On the server now'),
          if (current != null)
            _VersionTile(current, expected: v.expected, selected: false, onTap: null)
          else
            ListTile(
              contentPadding: EdgeInsets.zero,
              leading: const Icon(Icons.music_note_rounded),
              title: Text(v.currentUrl ?? 'Not downloaded yet'),
              trailing: v.currentUrl == null ? null : _CopyLink(v.currentUrl!),
            ),
          if (v.replacing != null)
            Text(
              'Being replaced with ${v.versions.where((x) => x.youtubeId == v.replacing).firstOrNull?.title ?? 'the version picked'}: '
              'it plays this one until that\'s downloaded.',
              style: const TextStyle(color: muted, height: 1.35),
            ),
          Row(children: [
            const Expanded(child: _Heading('Other versions')),
            TextButton.icon(
              onPressed: _searching ? null : () => _load(search: true),
              icon: _searching
                  ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.travel_explore_rounded, size: 18),
              label: Text(_searching ? 'Searching…' : 'Search YouTube again'),
            ),
          ]),
          if (others.isEmpty)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 8),
              child: Text('No other versions found yet. Search YouTube again, or paste a link below.', style: TextStyle(color: muted)),
            ),
          for (final x in others)
            _VersionTile(x, expected: v.expected, selected: _picked == x.youtubeId && _link.text.trim().isEmpty,
                onTap: () => setState(() {
                      _picked = x.youtubeId;
                      _link.clear();
                    })),
          const _Heading('Or paste a YouTube link'),
          TextField(
            controller: _link,
            keyboardType: TextInputType.url,
            decoration: const InputDecoration(hintText: 'https://music.youtube.com/watch?v=…'),
            onChanged: (_) => setState(() {}),
          ),
          const SizedBox(height: 20),
          GradientButton(
            label: _saving ? 'Replacing…' : 'Use this version',
            onTap: _choice == null || _saving ? null : _replace,
          ),
        ],
      ]),
    );
  }
}

String _clock(Duration d) => '${d.inMinutes}:${(d.inSeconds % 60).toString().padLeft(2, '0')}';

class _Heading extends StatelessWidget {
  const _Heading(this.text);
  final String text;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(top: 18, bottom: 6),
        child: Text(text, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 15)),
      );
}

class _VersionTile extends StatelessWidget {
  const _VersionTile(this.version, {required this.expected, required this.selected, required this.onTap});
  final SongVersion version;
  final Duration? expected;
  final bool selected;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final d = version.duration;
    final off = d == null || expected == null ? null : (d - expected!).inSeconds;
    final lengthColor = off == null ? muted : off.abs() <= 3 ? const Color(0xFF86EFAC) : off.abs() <= 10 ? accent3 : const Color(0xFFFDA4AF);
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Pressable(
        onTap: onTap,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 200),
          padding: const EdgeInsets.fromLTRB(14, 10, 4, 10),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(radiusM),
            gradient: selected ? LinearGradient(colors: [accent.withValues(alpha: 0.4), accent2.withValues(alpha: 0.3)]) : null,
            color: selected ? null : glassFill,
            border: Border.all(color: selected ? Colors.white.withValues(alpha: 0.4) : glassBorder),
          ),
          child: Row(children: [
            if (onTap != null) ...[
              Icon(selected ? Icons.radio_button_checked_rounded : Icons.radio_button_off_rounded, size: 20),
              const SizedBox(width: 12),
            ],
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(version.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700)),
                const SizedBox(height: 2),
                Text(version.channel.isEmpty ? 'YouTube' : version.channel,
                    maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 13)),
                const SizedBox(height: 2),
                if (d != null)
                  Text(off == null || off == 0 ? _clock(d) : '${_clock(d)} (${off > 0 ? '+' : ''}${off}s)',
                      style: TextStyle(color: lengthColor, fontSize: 12, fontWeight: FontWeight.w600)),
              ]),
            ),
            _CopyLink(version.url),
          ]),
        ),
      ),
    );
  }
}

/// Copies a version's YouTube link, to check it on YouTube first.
class _CopyLink extends StatelessWidget {
  const _CopyLink(this.url);
  final String url;

  @override
  Widget build(BuildContext context) => IconButton(
        tooltip: 'Copy the YouTube link',
        icon: const Icon(Icons.link_rounded, size: 20),
        onPressed: () {
          Clipboard.setData(ClipboardData(text: url));
          toast(context, 'Link copied: open it in YouTube to listen first.');
        },
      );
}
