import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/models.dart';
import '../state/data.dart';
import '../state/session.dart';
import 'catalog_album_screen.dart';
import 'fx/motion.dart';
import 'theme.dart';
import 'widgets.dart';

/// The bell on Home: new releases from the artists you follow (newest first). Opening it marks
/// them read; tapping one opens the album, ready to add.
class NotificationsScreen extends ConsumerStatefulWidget {
  const NotificationsScreen({super.key});

  @override
  ConsumerState<NotificationsScreen> createState() => _NotificationsScreenState();
}

class _NotificationsScreenState extends ConsumerState<NotificationsScreen> {
  bool _marked = false;

  void _markRead(Notifications n) {
    if (_marked || n.unread == 0) return;
    _marked = true;
    final api = ref.read(apiProvider);
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      try {
        await api?.markNotificationsRead();
        ref.invalidate(homeProvider); // the bell's count
      } catch (_) {
        // offline: they stay unread until next time
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final value = ref.watch(notificationsProvider);
    value.whenData(_markRead);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Notifications'),
        leading: Padding(padding: const EdgeInsets.all(6), child: GlassIconButton(icon: Icons.arrow_back_rounded, onPressed: () => Navigator.maybePop(context))),
      ),
      body: RefreshIndicator(
        color: accent,
        backgroundColor: surface,
        onRefresh: () async => ref.invalidate(notificationsProvider),
        child: value.when(
          loading: () => const ListSkeleton(rows: 6),
          error: (e, _) => ErrorView(e, onRetry: () => ref.invalidate(notificationsProvider)),
          data: (n) => n.items.isEmpty
              ? ListView(children: const [
                  SizedBox(height: 120),
                  Icon(Icons.notifications_none_rounded, size: 56, color: muted),
                  SizedBox(height: 14),
                  Padding(
                    padding: EdgeInsets.symmetric(horizontal: 40),
                    child: Text('When an artist you follow puts out something new, it shows up here.\n\n'
                        'Open an artist and tap Follow.', textAlign: TextAlign.center, style: TextStyle(color: muted, height: 1.4)),
                  ),
                ])
              : ListView.builder(
                  padding: const EdgeInsets.only(bottom: 190),
                  itemCount: n.items.length,
                  itemBuilder: (context, i) => FadeSlideIn(index: i, child: _NotificationTile(n.items[i])),
                ),
        ),
      ),
    );
  }
}

class _NotificationTile extends StatelessWidget {
  const _NotificationTile(this.n);
  final AppNotification n;

  @override
  Widget build(BuildContext context) {
    final album = n.album;
    return ListTile(
      contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 4),
      leading: Cover(album?.thumbUrl, size: 56, icon: Icons.album_rounded),
      title: Text(n.title, maxLines: 1, overflow: TextOverflow.ellipsis,
          style: TextStyle(fontWeight: n.read ? FontWeight.w500 : FontWeight.w800)),
      subtitle: Text([n.body, timeAgo(n.created)].where((s) => s.isNotEmpty).join(' · '),
          maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted)),
      trailing: n.read ? null : Container(width: 9, height: 9, decoration: const BoxDecoration(color: accent2, shape: BoxShape.circle)),
      onTap: album == null ? null : () => push(context, CatalogAlbumScreen(album)),
    );
  }
}
