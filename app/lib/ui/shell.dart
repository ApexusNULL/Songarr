import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../state/brand.dart';
import '../state/data.dart';
import '../state/jam.dart';
import '../state/session.dart';
import '../state/offline.dart';
import '../state/player.dart';
import '../state/push.dart';
import '../state/updates.dart';
import 'catalog_album_screen.dart';
import 'fx/motion.dart';
import 'fx/nebula.dart';
import 'home_screen.dart';
import 'jam_sheet.dart';
import 'library_screen.dart';
import 'search_screen.dart';
import 'theme.dart';
import 'widgets.dart';

/// Home / Search / Library, each with its own back stack, over the drifting nebula. The mini
/// player and the tab bar float above everything as glass.
class Shell extends ConsumerStatefulWidget {
  const Shell({super.key});
  @override
  ConsumerState<Shell> createState() => _ShellState();
}

class _ShellState extends ConsumerState<Shell> {
  int _tab = 0;
  final _navigators = List.generate(3, (_) => GlobalKey<NavigatorState>());
  StreamSubscription<String>? _messages;
  Timer? _episodeSync;
  AppLifecycleListener? _lifecycle;
  ProviderSubscription<JamState>? _jams;
  List<StreamSubscription<Object?>> _push = const [];
  bool _inviteOpen = false;

  @override
  void initState() {
    super.initState();
    _messages = ref.read(playerProvider).messages.listen((m) {
      if (mounted) toast(context, m);
    });
    // Pick up where you left off: the last song, paused, after closing, a force-close or an update.
    unawaited(ref.read(playerProvider).restoreLastSession());
    // Podcast downloads follow the server: on start, when the app comes back, and now and then.
    final offline = ref.read(offlineProvider.notifier);
    offline.syncEpisodes();
    _episodeSync = Timer.periodic(const Duration(minutes: 15), (_) => offline.syncEpisodes());
    final updater = ref.read(updaterProvider.notifier);
    updater.check();
    final brand = ref.read(brandProvider.notifier);
    brand.refresh(ref.read(apiProvider)); // the server's name and icon (Settings → Name and icon)
    _lifecycle = AppLifecycleListener(onResume: () {
      ref.refreshReleases(); // anything new from the artists you follow
      offline.syncEpisodes();
      brand.refresh(ref.read(apiProvider));
      updater.check(); // at most hourly
      ref.read(jamProvider.notifier).refresh();
    });
    Push.link(ref, openAlbum: (album) {
      // a tapped "New single from ..." notification: the album, in the tab that's showing
      _navigators[_tab].currentState?.push(MaterialPageRoute(builder: (_) => CatalogAlbumScreen(album)));
    }).then((subs) {
      if (mounted) {
        _push = subs;
      } else {
        for (final s in subs) {
          s.cancel();
        }
      }
    });
    // "Sam started a Jam": each invite pops up once, whichever screen is showing.
    _jams = ref.listenManual(jamProvider, (_, _) => WidgetsBinding.instance.addPostFrameCallback((_) => _announceInvites()),
        fireImmediately: true);
    // Android 13+ hides the playback notification and lock-screen player until allowed.
    if (Platform.isAndroid) {
      const MethodChannel('songarr/permissions').invokeMethod<void>('requestNotifications').catchError((_) {});
    }
  }

  Future<void> _announceInvites() async {
    if (!mounted || _inviteOpen) return;
    final jams = ref.read(jamProvider.notifier);
    final fresh = jams.takeNewInvites();
    if (fresh.isEmpty || ref.read(jamProvider).jam != null && fresh.every((i) => i.id == ref.read(jamProvider).jam!.id)) return;
    _inviteOpen = true;
    HapticFeedback.mediumImpact();
    try {
      await showJamInvite(context, ref, fresh.last);
    } finally {
      _inviteOpen = false;
    }
  }

  @override
  void dispose() {
    _jams?.close();
    for (final s in _push) {
      s.cancel();
    }
    _messages?.cancel();
    _episodeSync?.cancel();
    _lifecycle?.dispose();
    super.dispose();
  }

  Widget _tabNavigator(int i, Widget root) => HeroControllerScope.none(
        child: Navigator(
          key: _navigators[i],
          onGenerateRoute: (_) => MaterialPageRoute(builder: (_) => root),
        ),
      );

  void _select(int i) {
    HapticFeedback.selectionClick();
    if (i == _tab) {
      _navigators[i].currentState?.popUntil((r) => r.isFirst); // tap again: back to the top
    } else {
      setState(() => _tab = i);
    }
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (didPop) return;
        final nav = _navigators[_tab].currentState;
        if (nav != null && nav.canPop()) {
          nav.pop();
        } else if (_tab != 0) {
          setState(() => _tab = 0);
        }
      },
      child: Scaffold(
        backgroundColor: background,
        extendBody: true,
        body: Stack(children: [
          const Positioned.fill(child: NebulaBackground()),
          IndexedStack(index: _tab, children: [
            _tabNavigator(0, const HomeScreen()),
            _tabNavigator(1, const SearchScreen()),
            _tabNavigator(2, const LibraryScreen()),
          ]),
        ]),
        bottomNavigationBar: SafeArea(
          top: false,
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const MiniPlayer(),
            FloatingTabBar(index: _tab, onSelect: _select),
          ]),
        ),
      ),
    );
  }
}

class FloatingTabBar extends StatelessWidget {
  const FloatingTabBar({super.key, required this.index, required this.onSelect});
  final int index;
  final ValueChanged<int> onSelect;

  static const _items = [
    (Icons.blur_on_rounded, 'Home'),
    (Icons.search_rounded, 'Search'),
    (Icons.library_music_rounded, 'Library'),
  ];

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(24, 0, 24, 10),
        child: Glass(
          radius: 30,
          child: SizedBox(
            height: 60,
            child: LayoutBuilder(builder: (context, box) {
              final w = box.maxWidth / _items.length;
              return Stack(children: [
                // the glowing pill slides to the selected tab
                AnimatedPositioned(
                  duration: const Duration(milliseconds: 420),
                  curve: Curves.easeOutBack,
                  left: w * index + 8,
                  top: 8,
                  width: w - 16,
                  height: 44,
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(22),
                      gradient: LinearGradient(colors: [accent.withValues(alpha: 0.38), accent2.withValues(alpha: 0.26)]),
                      border: Border.all(color: Colors.white.withValues(alpha: 0.14)),
                      boxShadow: [BoxShadow(color: accent.withValues(alpha: 0.35), blurRadius: 16)],
                    ),
                  ),
                ),
                Row(children: [
                  for (var i = 0; i < _items.length; i++)
                    Expanded(
                      child: Semantics(
                        button: true,
                        selected: i == index,
                        label: _items[i].$2,
                        child: Pressable(
                          scale: 0.9,
                          onTap: () => onSelect(i),
                          child: SizedBox(
                            height: 60,
                            child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                              AnimatedScale(
                                scale: i == index ? 1.1 : 1.0,
                                duration: const Duration(milliseconds: 300),
                                child: Icon(_items[i].$1, color: i == index ? Colors.white : muted, size: 24),
                              ),
                              AnimatedSize(
                                duration: const Duration(milliseconds: 300),
                                curve: Curves.easeOutCubic,
                                child: i == index
                                    ? Padding(
                                        padding: const EdgeInsets.only(left: 6),
                                        child: Text(_items[i].$2, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13)),
                                      )
                                    : const SizedBox.shrink(),
                              ),
                            ]),
                          ),
                        ),
                      ),
                    ),
                ]),
              ]);
            }),
          ),
        ),
      );
}
