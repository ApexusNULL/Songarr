import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import '../api/client.dart';
import '../state/brand.dart';
import '../state/session.dart';
import 'fx/motion.dart';
import 'fx/nebula.dart';
import 'fx/sparkle.dart';
import 'theme.dart';

enum _Way { scan, code, password }

/// First run: sign in by scanning the QR code from Songarr → People → "Pair a phone or computer",
/// by typing the server address and that code, or with a sign-in name and password.
class PairScreen extends ConsumerStatefulWidget {
  const PairScreen({super.key});
  @override
  ConsumerState<PairScreen> createState() => _PairScreenState();
}

class _PairScreenState extends ConsumerState<PairScreen> {
  final _server = TextEditingController();
  final _code = TextEditingController();
  final _login = TextEditingController();
  final _password = TextEditingController();
  bool _busy = false;
  bool _showPassword = false;
  // desktops usually have no camera for this
  _Way _way = Platform.isAndroid || Platform.isIOS ? _Way.scan : _Way.password;
  String? _error;

  @override
  void initState() {
    super.initState();
    SessionNotifier.lastServer().then((s) {
      if (s != null && mounted && _server.text.isEmpty) _server.text = s;
    });
  }

  @override
  void dispose() {
    for (final c in [_server, _code, _login, _password]) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _run(Future<void> Function() signIn) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await signIn();
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } catch (e) {
      setState(() => _error = 'Couldn\'t sign in: $e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _pair(String server, String code) {
    final s = PairingInfo.normalizeServer(server);
    if (s.isEmpty) {
      setState(() {
        _error = 'This code has no server address. In Songarr, set Settings → App access, then make a new code.';
        _way = _Way.code;
        _code.text = code;
      });
      return;
    }
    _run(() => ref.read(sessionProvider.notifier).pair(s, code));
  }

  void _signIn() {
    final s = PairingInfo.normalizeServer(_server.text);
    if (s.isEmpty || _login.text.trim().isEmpty || _password.text.isEmpty) {
      setState(() => _error = 'Fill in the server address, your sign-in name and your password.');
      return;
    }
    _run(() => ref.read(sessionProvider.notifier).signIn(s, _login.text.trim(), _password.text));
  }

  String _name() => ref.watch(brandProvider).name;

  String get _hint => switch (_way) {
        _Way.scan => 'On your PC, open ${_name()} → People → "Pair a phone or computer", then scan the code.',
        _Way.code => 'Type the server address and the code shown in ${_name()} → People → "Pair a phone or computer".',
        _Way.password => 'Use the name and password set for you in ${_name()} → People, or in Settings on a device that\'s already signed in.',
      };

  Widget _button(String label, VoidCallback onTap) => Pressable(
        onTap: _busy ? null : onTap,
        child: Container(
          height: 52,
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(26),
            gradient: aurora,
            boxShadow: [BoxShadow(color: accent2.withValues(alpha: 0.4), blurRadius: 20)],
          ),
          alignment: Alignment.center,
          child: Text(label, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 16)),
        ),
      );

  Widget _serverField() => TextField(
        controller: _server,
        keyboardType: TextInputType.url,
        autocorrect: false,
        textInputAction: TextInputAction.next,
        decoration: const InputDecoration(labelText: 'Server address', hintText: 'https://music.yourdomain.com'),
      );

  Widget _form() => switch (_way) {
        _Way.scan => Glass(
            key: const ValueKey('scan'),
            padding: const EdgeInsets.all(10),
            child: AspectRatio(
              aspectRatio: 1,
              child: ClipRRect(
                borderRadius: BorderRadius.circular(radiusM),
                child: MobileScanner(
                  onDetect: (capture) {
                    for (final b in capture.barcodes) {
                      final info = b.rawValue == null ? null : PairingInfo.fromQr(b.rawValue!);
                      if (info != null) {
                        _pair(info.server, info.code);
                        return;
                      }
                    }
                  },
                  errorBuilder: (context, error) => Center(
                    child: Padding(
                      padding: const EdgeInsets.all(16),
                      child: Text('Camera unavailable (${error.errorCode.name}). Type the code or use a password instead.',
                          textAlign: TextAlign.center),
                    ),
                  ),
                ),
              ),
            ),
          ),
        _Way.code => Glass(
            key: const ValueKey('code'),
            padding: const EdgeInsets.all(18),
            child: Column(children: [
              _serverField(),
              const SizedBox(height: 12),
              TextField(
                controller: _code,
                autocorrect: false,
                textCapitalization: TextCapitalization.characters,
                decoration: const InputDecoration(labelText: 'Pairing code', hintText: 'ABCDE-FGHJK'),
                onSubmitted: (_) => _pair(_server.text, _code.text),
              ),
              const SizedBox(height: 18),
              _button('Sign in', () => _pair(_server.text, _code.text)),
            ]),
          ),
        _Way.password => Glass(
            key: const ValueKey('password'),
            padding: const EdgeInsets.all(18),
            child: AutofillGroup(
              child: Column(children: [
                _serverField(),
                const SizedBox(height: 12),
                TextField(
                  controller: _login,
                  autocorrect: false,
                  textInputAction: TextInputAction.next,
                  autofillHints: const [AutofillHints.username],
                  decoration: const InputDecoration(labelText: 'Sign-in name'),
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: _password,
                  obscureText: !_showPassword,
                  autocorrect: false,
                  enableSuggestions: false,
                  autofillHints: const [AutofillHints.password],
                  decoration: InputDecoration(
                    labelText: 'Password',
                    suffixIcon: IconButton(
                      tooltip: _showPassword ? 'Hide password' : 'Show password',
                      icon: Icon(_showPassword ? Icons.visibility_off_rounded : Icons.visibility_rounded),
                      onPressed: () => setState(() => _showPassword = !_showPassword),
                    ),
                  ),
                  onSubmitted: (_) => _signIn(),
                ),
                const SizedBox(height: 18),
                _button('Sign in', _signIn),
              ]),
            ),
          ),
      };

  @override
  Widget build(BuildContext context) {
    final camera = Platform.isAndroid || Platform.isIOS;
    final others = [
      if (camera && _way != _Way.scan) (_Way.scan, Icons.qr_code_scanner_rounded, 'Scan a QR code'),
      if (_way != _Way.password) (_Way.password, Icons.password_rounded, 'Sign in with a password'),
      if (_way != _Way.code) (_Way.code, Icons.keyboard_rounded, 'Type a pairing code'),
    ];
    return Scaffold(
      backgroundColor: background,
      body: Stack(children: [
        const Positioned.fill(child: NebulaBackground()),
        const Positioned(left: 0, right: 0, top: 0, height: 320, child: AmbientSparkles(perSecond: 6)),
        SafeArea(
          child: ListView(padding: const EdgeInsets.all(24), children: [
            const SizedBox(height: 28),
            FadeSlideIn(
              child: Center(
                child: BrandMark(gradient: aurora, glow: accent2.withValues(alpha: 0.5)),
              ),
            ),
            const SizedBox(height: 18),
            FadeSlideIn(
              index: 1,
              child: GradientMask(
                child: Text(ref.watch(brandProvider).name, textAlign: TextAlign.center, style: Theme.of(context).textTheme.headlineLarge?.copyWith(fontSize: 40)),
              ),
            ),
            const SizedBox(height: 8),
            FadeSlideIn(
              index: 2,
              child: AnimatedSwitcher(
                duration: const Duration(milliseconds: 250),
                child: Text(_hint, key: ValueKey(_way), textAlign: TextAlign.center, style: const TextStyle(color: muted, height: 1.4)),
              ),
            ),
            const SizedBox(height: 24),
            FadeSlideIn(index: 3, child: AnimatedSwitcher(duration: const Duration(milliseconds: 350), child: _form())),
            const SizedBox(height: 12),
            if (_busy) const Center(child: CircularProgressIndicator()),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(_error!, textAlign: TextAlign.center, style: const TextStyle(color: Color(0xFFFDA4AF))),
              ),
            const SizedBox(height: 4),
            for (final (way, icon, label) in others)
              Center(
                child: TextButton.icon(
                  icon: Icon(icon, size: 18),
                  label: Text(label),
                  onPressed: () => setState(() {
                    _way = way;
                    _error = null;
                  }),
                ),
              ),
          ]),
        ),
      ]),
    );
  }
}
