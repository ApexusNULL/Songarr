import 'dart:io';

import 'package:audio_service/audio_service.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'state/audio_handler.dart';
import 'state/brand.dart';
import 'state/offline.dart';
import 'state/push.dart';
import 'state/session.dart';
import 'ui/pair_screen.dart';
import 'ui/shell.dart';
import 'ui/theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  // The media session: background playback, the notification and lock-screen player, and
  // controls from headphones, Bluetooth and cars.
  final SongarrAudioHandler handler;
  if (Platform.isAndroid || Platform.isIOS || Platform.isMacOS) {
    handler = await AudioService.init(
      builder: SongarrAudioHandler.new,
      config: const AudioServiceConfig(
        androidNotificationChannelId: 'com.songarr.app.playback',
        androidNotificationChannelName: 'Playback',
        androidNotificationChannelDescription: 'What\'s playing, with controls',
        androidNotificationIcon: 'drawable/ic_stat_songarr',
        notificationColor: accent,
        androidNotificationOngoing: true,
        androidStopForegroundOnPause: true,
        fastForwardInterval: Duration(seconds: 30),
        rewindInterval: Duration(seconds: 10),
      ),
    );
  } else {
    handler = SongarrAudioHandler(); // desktop: no system media session yet
  }
  await Push.init(); // Jam invite notifications
  // No automatic retries: a signed-out device or an unreachable server should show at once.
  runApp(ProviderScope(
    retry: (_, _) => null,
    overrides: [audioHandlerProvider.overrideWithValue(handler)],
    child: const SongarrApp(),
  ));
}

class SongarrApp extends ConsumerWidget {
  const SongarrApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    ref.watch(offlineProvider); // start loading downloads and the cache index early
    return MaterialApp(
      title: ref.watch(brandProvider).name, // the recent-apps list
      debugShowCheckedModeBanner: false,
      theme: songarrTheme(),
      color: background,
      builder: (context, child) => ColoredBox(color: background, child: child), // pages are see-through
      home: const _Root(),
    );
  }
}

class _Root extends ConsumerWidget {
  const _Root();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(sessionProvider);
    return session.when(
      loading: () => const Scaffold(body: Center(child: CircularProgressIndicator())),
      error: (e, _) => Scaffold(body: Center(child: Text('$e'))),
      data: (s) => s == null ? const PairScreen() : Shell(key: ValueKey(s.token)),
    );
  }
}
