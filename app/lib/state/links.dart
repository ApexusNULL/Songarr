import 'dart:async';
import 'dart:math';

import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/client.dart';
import '../api/models.dart';
import 'player.dart';
import 'session.dart';

/// Playing from another app (the fitness app's workout music, say):
///
///     songarr://play?list=liked|<playlist id>[&track=<song id>][&shuffle=1][&back=1]
///     songarr://play?album=<album id>[&track=...]     songarr://play?track=<song id>
///
/// The list plays from [track] (or the start, or shuffled), as if picked in the app. Links arrive
/// through PlayLinkActivity, which hands them to the running player without showing Songarr; when
/// Songarr had to open for one, `back=1` returns to the app that asked. A list this profile doesn't
/// have says so.
class PlayLinks {
  static const _ch = MethodChannel('songarr/links');

  /// Handle the link that opened the app, and any that arrive while it runs. [restored] is the last
  /// session being put back: a link plays after it, so it isn't overwritten.
  static void listen(WidgetRef ref, Future<void> restored) {
    _ch.setMethodCallHandler((call) async {
      if (call.method == 'open' && call.arguments is String) await open(ref, Uri.parse(call.arguments as String), restored);
    });
    unawaited(_initial(ref, restored));
  }

  /// A link that opened Songarr. The app can start before Android has set up its side of the
  /// channel, so ask again for a few seconds until it answers.
  static Future<void> _initial(WidgetRef ref, Future<void> restored) async {
    for (var i = 0; i < 20; i++) {
      try {
        final link = await _ch.invokeMethod<String>('initial');
        if (link != null) await open(ref, Uri.parse(link), restored);
        return;
      } on MissingPluginException {
        await Future<void>.delayed(const Duration(milliseconds: 250));
      } catch (_) {
        return;
      }
    }
  }

  static void stop() => _ch.setMethodCallHandler(null);

  static Future<void> open(WidgetRef ref, Uri uri, Future<void> restored) async {
    if (uri.scheme != 'songarr' || uri.host != 'play') return;
    final player = ref.read(playerProvider);
    final api = ref.read(apiProvider);
    if (api == null) {
      player.say('Sign in to Songarr first, then try again.');
      return;
    }
    await restored.catchError((_) {});
    final q = uri.queryParameters;
    final list = q['list'], album = q['album'], trackId = q['track'];
    try {
      final (List<Track> tracks, String? listKey) = list == 'liked'
          ? ((await api.liked()).tracks, 'liked')
          : list != null
              ? ((await api.playlist(list)).tracks, list)
              : album != null
                  ? ((await api.album(album)).tracks, null)
                  : trackId != null
                      ? ([await api.track(trackId)], null)
                      : (const <Track>[], null);
      if (tracks.isEmpty) {
        player.say(list != null || album != null ? 'That list is empty.' : 'Nothing to play.');
        return;
      }
      final index = trackId == null ? 0 : max(0, tracks.indexWhere((t) => t.id == trackId));
      await player.play(tracks, index: index, shuffle: q['shuffle'] == '1' && trackId == null, listKey: listKey);
      if (q['back'] == '1') unawaited(_ch.invokeMethod('back').catchError((_) => false));
    } on ApiException catch (e) {
      player.say(e.status == 404 ? 'That list isn\'t in this Songarr profile.' : e.message);
    }
  }
}
