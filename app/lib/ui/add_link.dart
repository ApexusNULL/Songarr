import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/client.dart';
import '../state/data.dart';
import '../state/session.dart';
import 'fx/motion.dart';
import 'theme.dart';
import 'widgets.dart';

final _link = RegExp(r'https?://\S+');

/// The link in some text (shared text often has words around it), or null.
String? linkIn(String text) => _link.firstMatch(text)?.group(0);

/// "Add from a link" (a playlist's menu): asks for the link, with one already copied filled in.
Future<void> askForLink(BuildContext context, WidgetRef ref, {String? playlistId, String? playlistName}) async {
  String copied = '';
  try {
    copied = linkIn((await Clipboard.getData(Clipboard.kTextPlain))?.text ?? '') ?? '';
  } catch (_) {
    // no clipboard to read: an empty box
  }
  if (!context.mounted) return;
  final text = await askText(context, 'Add from a link', 'A YouTube link, or any song link', initial: copied);
  final url = text == null ? null : linkIn(text);
  if (url == null || !context.mounted) return;
  await addFromLink(context, ref, url, playlistId: playlistId, playlistName: playlistName);
}

/// A song from a link (YouTube, or anything else yt-dlp can download): the server reads it and that
/// very upload downloads next. Onto [playlistId]; or (from Search, or shared from another app) into
/// your library, with a playlist to pick for it.
Future<void> addFromLink(BuildContext context, WidgetRef ref, String url, {String? playlistId, String? playlistName}) async {
  final api = ref.read(apiProvider);
  if (api == null) return;
  toast(context, 'Reading the link…');
  try {
    final track = await api.addLink(url, playlistId: playlistId);
    ref.refreshLibrary(); // (and the playlist)
    if (!context.mounted) return;
    final when = track.playable ? '' : '; it downloads next';
    if (playlistId != null) {
      toast(context, 'Added "${track.title}" to ${playlistName ?? 'the playlist'}$when');
      return;
    }
    toast(context, '"${track.title}" is in your library$when');
    await addToPlaylistDialog(context, ref, [track], heading: '${track.title} · ${track.artistLine}');
  } on ApiException catch (e) {
    if (context.mounted) toast(context, e.message);
  }
}

/// In Search, for a pasted link: add what it points to instead of searching for it.
class LinkCard extends ConsumerWidget {
  const LinkCard(this.url, {super.key});
  final String url;

  @override
  Widget build(BuildContext context, WidgetRef ref) => Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
        child: Pressable(
          scale: 0.98,
          onTap: () => addFromLink(context, ref, url),
          child: Glass(
            blur: false,
            radius: radiusM,
            padding: const EdgeInsets.all(16),
            child: Row(children: [
              const GradientMask(child: Icon(Icons.add_link_rounded, size: 30)),
              const SizedBox(width: 14),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  const Text('Add the song at this link', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 15.5)),
                  const SizedBox(height: 3),
                  Text(url, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 13)),
                  const SizedBox(height: 3),
                  const Text('YouTube, or any site yt-dlp can download from. That upload downloads to your server.',
                      style: TextStyle(color: muted, fontSize: 12.5)),
                ]),
              ),
            ]),
          ),
        ),
      );
}
