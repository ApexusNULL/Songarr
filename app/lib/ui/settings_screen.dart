import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../state/brand.dart';
import '../state/data.dart';
import '../state/offline.dart';
import '../state/player.dart';
import '../state/session.dart';
import '../state/updates.dart';
import 'fx/motion.dart';
import 'fx/tilt.dart';
import 'theme.dart';
import 'widgets.dart';

const _gb = 1024 * 1024 * 1024;

class SettingsScreen extends ConsumerWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(sessionProvider).value;
    final me = ref.watch(meProvider);
    final offline = ref.watch(offlineProvider);
    final store = ref.read(offlineProvider.notifier);
    return Scaffold(
      appBar: AppBar(
        leading: Padding(padding: const EdgeInsets.all(6), child: GlassIconButton(icon: Icons.arrow_back_rounded, onPressed: () => Navigator.maybePop(context))),
        title: const Text('Settings'),
      ),
      body: ListView(padding: const EdgeInsets.fromLTRB(16, 8, 16, 190), children: [
        _Section('Account', [
          ListTile(
            leading: const GradientMask(child: Icon(Icons.person_rounded)),
            title: Text(session?.userName ?? '', style: const TextStyle(fontWeight: FontWeight.w700)),
            subtitle: Text(me.whenOrNull(data: (m) => m.spotifyAccounts.isEmpty ? 'No Spotify account linked' : 'Spotify: ${m.spotifyAccounts.join(', ')}') ?? ''),
          ),
          ListTile(
            leading: ref.watch(brandProvider).icon == null
                ? const GradientMask(child: Icon(Icons.dns_rounded))
                : const BrandMark(size: 30, gradient: aurora), // the icon chosen on the server
            title: const Text('Server', style: TextStyle(fontWeight: FontWeight.w700)),
            subtitle: Text('${session?.server ?? ''}${me.whenOrNull(data: (m) => ' · ${ref.watch(brandProvider).name} ${m.serverVersion}') ?? ''}'),
          ),
        ]),
        _Section('Signing in', [const _PasswordTile()]),
        _Section('Storage on this device', [
          ListTile(
            leading: const GradientMask(child: Icon(Icons.download_done_rounded)),
            title: const Text('Downloads', style: TextStyle(fontWeight: FontWeight.w700)),
            subtitle: Text('${offline.downloads.length} songs · ${formatBytes(offline.downloadBytes)} · kept until you remove them'),
            trailing: offline.downloads.isEmpty
                ? null
                : TextButton(
                    onPressed: () => _confirm(context, 'Remove all downloads?', 'Songs stay in your library and can still be streamed.',
                        () => store.remove(offline.downloads.keys.toList())),
                    child: const Text('Remove all'),
                  ),
          ),
          ListTile(
            leading: const GradientMask(child: Icon(Icons.podcasts_rounded)),
            title: const Text('Podcast episodes', style: TextStyle(fontWeight: FontWeight.w700)),
            subtitle: Text('${offline.episodes.length} on this phone · ${formatBytes(offline.episodeBytes)} · '
                'removed once played or when their days run out (set per show)'),
          ),
          ListTile(
            leading: const GradientMask(child: Icon(Icons.cached_rounded)),
            title: const Text('Listening cache', style: TextStyle(fontWeight: FontWeight.w700)),
            subtitle: Text('${formatBytes(offline.cacheBytes)} of ${formatBytes(offline.cacheLimit)}. '
                'Songs are kept after you play them; when it\'s full, the ones you\'ve listened to least over time go first.'),
            isThreeLine: true,
            trailing: TextButton(onPressed: store.clearCache, child: const Text('Clear')),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 14),
            child: Wrap(spacing: 8, runSpacing: 8, children: [
              for (final g in [1, 2, 5, 10, 20])
                Pressable(
                  onTap: () => store.setCacheLimit(g * _gb),
                  child: AnimatedContainer(
                    duration: const Duration(milliseconds: 250),
                    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(18),
                      gradient: offline.cacheLimit == g * _gb ? auroraSoft : null,
                      border: Border.all(color: glassBorder),
                    ),
                    child: Text('$g GB', style: const TextStyle(fontWeight: FontWeight.w700)),
                  ),
                ),
            ]),
          ),
        ]),
        if (Tilt.supported) _Section('Look & feel', [const _TiltTile()]),
        _Section('App', [const _AppVersion()]),
        _Section('This device', [
          ListTile(
            leading: const Icon(Icons.logout_rounded, color: Color(0xFFFDA4AF)),
            title: const Text('Sign out', style: TextStyle(color: Color(0xFFFDA4AF), fontWeight: FontWeight.w700)),
            subtitle: const Text('Removes this device from Songarr and deletes its downloads and cache.'),
            onTap: () => _confirm(context, 'Sign out?', 'Downloads and cached songs on this device are deleted.', () async {
              await ref.read(playerProvider).stop();
              await store.wipe();
              await ref.read(sessionProvider.notifier).signOut();
            }),
          ),
        ]),
      ]),
    );
  }

  Future<void> _confirm(BuildContext context, String title, String body, Future<void> Function() action) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (d) => AlertDialog(
        title: Text(title),
        content: Text(body),
        actions: [
          TextButton(onPressed: () => Navigator.pop(d, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(d, true), child: const Text('OK')),
        ],
      ),
    );
    if (ok == true) await action();
  }
}

class _AppVersion extends ConsumerWidget {
  const _AppVersion();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final u = ref.watch(updaterProvider);
    final updater = ref.read(updaterProvider.notifier);
    final update = u.update;
    final status = u.checking && update == null
        ? 'Checking for updates…'
        : update == null
            ? (u.error ?? 'Up to date')
            : u.ready
                ? 'Version ${update.version} is ready to install'
                : 'Downloading version ${update.version}…';
    return ListTile(
      leading: const GradientMask(child: Icon(Icons.system_update_rounded)),
      title: Text('${ref.watch(brandProvider).name} ${u.version.isEmpty ? '' : u.version}', style: const TextStyle(fontWeight: FontWeight.w700)),
      subtitle: Text('$status\nUpdates come from your ${ref.watch(brandProvider).name} server.'),
      isThreeLine: true,
      trailing: u.ready
          ? FilledButton(
              onPressed: () async {
                final msg = await updater.install();
                if (msg != null && context.mounted) toast(context, msg);
              },
              child: const Text('Install'),
            )
          : TextButton(onPressed: u.checking ? null : () => updater.check(force: true), child: const Text('Check')),
    );
  }
}

/// A password for signing in on other devices by name, without a QR code from the PC.
class _PasswordTile extends ConsumerWidget {
  const _PasswordTile();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final account = ref.watch(accountProvider);
    final a = account.value;
    return ListTile(
      leading: const GradientMask(child: Icon(Icons.password_rounded)),
      title: Text(a?.hasPassword == true ? 'Password' : 'Set a password', style: const TextStyle(fontWeight: FontWeight.w700)),
      subtitle: Text(account.when(
        data: (a) => a.hasPassword
            ? 'Sign in on other devices as "${a.login}". Tap to change it.'
            : 'So you can sign in on other devices with your name and a password, no QR code needed.',
        loading: () => '…',
        error: (e, _) => 'Couldn\'t check: $e',
      )),
      onTap: a == null ? null : () => _passwordSheet(context, ref, a),
    );
  }

  Future<void> _passwordSheet(BuildContext context, WidgetRef ref, Account a) => showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        useRootNavigator: true,
        builder: (_) => _PasswordForm(account: a),
      );
}

class _PasswordForm extends ConsumerStatefulWidget {
  const _PasswordForm({required this.account});
  final Account account;
  @override
  ConsumerState<_PasswordForm> createState() => _PasswordFormState();
}

class _PasswordFormState extends ConsumerState<_PasswordForm> {
  late final _login = TextEditingController(text: widget.account.login ?? widget.account.name);
  final _current = TextEditingController();
  final _new = TextEditingController();
  final _again = TextEditingController();
  bool _busy = false;
  bool _show = false;
  String? _error;

  Future<void> _save() async {
    if (_new.text != _again.text) return setState(() => _error = 'The two new passwords don\'t match.');
    if (_new.text.length < 8) return setState(() => _error = 'Use at least 8 characters.');
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(apiProvider)!.setPassword(_login.text.trim(), _new.text, current: _current.text);
      ref.invalidate(accountProvider);
      if (mounted) {
        Navigator.pop(context);
        toast(context, 'Password saved. Sign in elsewhere as "${_login.text.trim()}".');
      }
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final change = widget.account.hasPassword;
    InputDecoration field(String label, {String? hint}) => InputDecoration(
          labelText: label,
          hintText: hint,
          suffixIcon: IconButton(
            tooltip: _show ? 'Hide' : 'Show',
            icon: Icon(_show ? Icons.visibility_off_rounded : Icons.visibility_rounded),
            onPressed: () => setState(() => _show = !_show),
          ),
        );
    return Padding(
      padding: EdgeInsets.fromLTRB(20, 0, 20, 20 + MediaQuery.viewInsetsOf(context).bottom),
      child: SafeArea(
        child: AutofillGroup(
          child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Text(change ? 'Change your password' : 'Set a password', style: Theme.of(context).textTheme.headlineMedium),
            const SizedBox(height: 6),
            const Text('Then on another phone or computer, choose "Sign in with a password" and use the same server address as here.',
                style: TextStyle(color: muted, height: 1.35)),
            const SizedBox(height: 16),
            TextField(
              controller: _login,
              autocorrect: false,
              autofillHints: const [AutofillHints.username],
              decoration: const InputDecoration(labelText: 'Sign-in name'),
            ),
            if (change) ...[
              const SizedBox(height: 12),
              TextField(
                controller: _current,
                obscureText: !_show,
                autofillHints: const [AutofillHints.password],
                decoration: field('Current password'),
              ),
            ],
            const SizedBox(height: 12),
            TextField(
              controller: _new,
              obscureText: !_show,
              autofillHints: const [AutofillHints.newPassword],
              decoration: field('New password', hint: 'At least 8 characters'),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _again,
              obscureText: !_show,
              autofillHints: const [AutofillHints.newPassword],
              decoration: field('New password again'),
              onSubmitted: (_) => _save(),
            ),
            if (_error != null)
              Padding(padding: const EdgeInsets.only(top: 10), child: Text(_error!, style: const TextStyle(color: Color(0xFFFDA4AF)))),
            const SizedBox(height: 18),
            FilledButton(
              onPressed: _busy ? null : _save,
              child: Padding(padding: const EdgeInsets.symmetric(vertical: 12), child: Text(_busy ? 'Saving…' : 'Save password')),
            ),
          ]),
        ),
      ),
    );
  }
}

/// Depth effect: the stars and sparkles shift as you turn your phone.
class _TiltTile extends StatefulWidget {
  const _TiltTile();
  @override
  State<_TiltTile> createState() => _TiltTileState();
}

class _TiltTileState extends State<_TiltTile> {
  @override
  Widget build(BuildContext context) => SwitchListTile(
        secondary: const GradientMask(child: Icon(Icons.threed_rotation_rounded)),
        title: const Text('Depth effect', style: TextStyle(fontWeight: FontWeight.w700)),
        subtitle: Text(MediaQuery.disableAnimationsOf(context)
            ? 'Off while your phone is set to remove animations'
            : 'The stars and sparkles shift as you turn your phone, like looking into space'),
        value: Tilt.instance.enabled,
        onChanged: (v) => setState(() => Tilt.instance.enabled = v),
      );
}

class _Section extends StatelessWidget {
  const _Section(this.title, this.children);
  final String title;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(top: 14),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(8, 6, 8, 8),
            child: GradientMask(child: Text(title.toUpperCase(), style: const TextStyle(fontWeight: FontWeight.w800, letterSpacing: 1.6, fontSize: 12))),
          ),
          Glass(blur: false, child: Column(children: children)),
        ]),
      );
}
