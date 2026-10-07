import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/client.dart';
import '../api/models.dart';
import 'session.dart';

/// The name and icon chosen on the server (Settings → Name and icon), remembered between launches
/// so the sign-in screen shows them too. Until a server has said, it's the name this build was
/// made with ([Brand.builtIn]).
class BrandNotifier extends Notifier<Brand> {
  static const _key = 'brand';

  @override
  Brand build() {
    _load();
    return const Brand();
  }

  Future<void> _load() async {
    try {
      final raw = (await SharedPreferences.getInstance()).getString(_key);
      if (raw != null) _set(Brand.fromJson(jsonDecode(raw) as Map<String, dynamic>));
    } catch (_) {
      // nothing remembered (or unreadable): the built-in name
    }
    // A build made with a chosen name and icon, before any server has said: its own home-screen icon.
    if (state.icon == null && Brand.builtIn != 'Songarr' && Platform.isAndroid) {
      try {
        final png = await const MethodChannel('songarr/brand').invokeMethod<Uint8List>('appIcon');
        if (png != null && state.icon == null) _set(Brand(name: state.name, icon: png, seeThrough: false));
      } catch (_) {
        // an older Android build without the channel
      }
    }
  }

  void _set(Brand b) {
    Brand.current = b.name;
    state = b;
  }

  /// Ask the server for its name and icon (at start and when the app comes back to the front).
  Future<void> refresh(SongarrApi? api) async {
    if (api == null) return;
    try {
      final server = (await api.me()).server;
      final servers = [for (final s in server['servers'] as List? ?? const []) if (s is String && s.isNotEmpty) s];
      if (servers.isNotEmpty) await ref.read(sessionProvider.notifier).learnServers(servers);
      final iconId = server['icon'] as String?;
      var icon = state.icon;
      if (iconId == null) {
        icon = null;
      } else if (iconId != state.iconId || icon == null) {
        icon = await api.brandIcon(256);
      }
      final name = (server['name'] as String? ?? '').trim();
      final b = Brand(name: name.isEmpty ? Brand.builtIn : name, iconId: iconId, icon: icon,
          seeThrough: server['see_through'] as bool? ?? true);
      _set(b);
      (await SharedPreferences.getInstance()).setString(_key, jsonEncode(b.toJson()));
    } catch (_) {
      // offline, or an older server: keep what we have
    }
  }
}

final brandProvider = NotifierProvider<BrandNotifier, Brand>(BrandNotifier.new);

/// The server's icon in a circle (Songarr's note on the aurora gradient until one is chosen).
class BrandMark extends ConsumerWidget {
  const BrandMark({super.key, this.size = 96, required this.gradient, this.glow});

  final double size;
  final Gradient gradient;
  final Color? glow;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final b = ref.watch(brandProvider);
    final icon = b.icon;
    return Container(
      width: size,
      height: size,
      clipBehavior: Clip.antiAlias,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        gradient: icon == null || b.seeThrough ? gradient : null,
        boxShadow: glow == null ? null : [BoxShadow(color: glow!, blurRadius: 40)],
      ),
      child: icon == null
          ? Icon(Icons.music_note_rounded, size: size * 0.54)
          : Padding(
              padding: EdgeInsets.all(b.seeThrough ? size * 0.16 : 0),
              child: Image.memory(icon, fit: b.seeThrough ? BoxFit.contain : BoxFit.cover, gaplessPlayback: true),
            ),
    );
  }
}
