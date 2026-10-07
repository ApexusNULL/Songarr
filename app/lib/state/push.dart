import 'dart:async';
import 'dart:io';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/models.dart';
import 'data.dart';
import 'jam.dart';
import 'session.dart';

/// Jam invites and new releases from artists you follow as notifications (Firebase Cloud
/// Messaging), so they reach you with the app closed. The server sends them; Android shows them;
/// tapping one opens the invite, or the album.
class Push {
  static bool _ready = false;

  /// Once, at start-up. Without Firebase (desktop, or a build without google-services.json),
  /// invites still arrive in the app while it's open.
  static Future<void> init() async {
    if (!Platform.isAndroid) return;
    try {
      await Firebase.initializeApp();
      _ready = true;
    } catch (_) {
      _ready = false;
    }
  }

  /// While signed in: tell the server where to reach this phone, and open invites as they come.
  static Future<List<StreamSubscription<Object?>>> link(WidgetRef ref, {void Function(CatalogAlbum album)? openAlbum}) async {
    if (!_ready) return const [];
    final messaging = FirebaseMessaging.instance;
    Future<void> register(String? token) async {
      if (token == null) return;
      try {
        await ref.read(apiProvider)?.setPushToken(token);
      } catch (_) {
        // offline: we'll register on the next start
      }
    }

    void open(RemoteMessage m) {
      if (m.data['type'] == 'new_release') {
        ref.refreshReleases();
        openAlbum?.call(CatalogAlbum({
          'source': 'deezer',
          'id': m.data['album'],
          'name': m.data['name'] ?? '',
          'artists': [?m.data['artist']],
          'type': m.data['kind'] ?? 'album',
          'thumb_url': m.data['thumb'],
        }));
        return;
      }
      if (m.data['type'] != 'jam_invite') return;
      final jams = ref.read(jamProvider.notifier);
      final id = m.data['jam'];
      if (id is String) jams.announceAgain(id); // show it even if the app already saw it
      jams.refresh();
    }

    void arrived(RemoteMessage m) {
      if (m.data['type'] == 'new_release') { // the app is open: the bell and Home catch up
        ref.refreshReleases();
        return;
      }
      open(m);
    }

    try {
      unawaited(messaging.getToken().then(register));
      final initial = await messaging.getInitialMessage(); // the app was opened from an invite
      if (initial != null) open(initial);
    } catch (_) {
      // Google Play services missing or not reachable: invites still arrive in the app
    }
    return [
      messaging.onTokenRefresh.listen(register),
      FirebaseMessaging.onMessage.listen(arrived), // app open: show the invite in the app
      FirebaseMessaging.onMessageOpenedApp.listen(open), // tapped the notification
    ];
  }
}
