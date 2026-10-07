import 'dart:async';
import 'dart:io';
import 'dart:math';

import 'package:flutter/widgets.dart';
import 'package:sensors_plus/sensors_plus.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Which way the phone is being turned, for the depth effect: stars and clouds behind the screen
/// drift one way and sparkles in front drift the other, like looking through a window into space.
///
/// It follows the gyroscope relative to however you're holding the phone: turning it moves the
/// view, and the view settles back to centre a couple of seconds after you hold still, so lying
/// down or holding it at an angle needs no "level". [value] is about 1 for a 25° turn (see
/// [response]) on each axis: x when the phone turns to the right (right edge away from you), y when
/// it tips back (top edge toward you).
///
/// The sensor only runs while something on screen uses it ([attach]) and the app is in front.
class Tilt extends ValueNotifier<Offset> with WidgetsBindingObserver {
  Tilt._() : super(Offset.zero) {
    WidgetsBinding.instance.addObserver(this);
    SharedPreferences.getInstance().then((p) => enabled = p.getBool(_pref) ?? true).catchError((_) => true);
  }

  static final instance = Tilt._();
  static const _pref = 'tilt_effects';
  static const _unit = 0.40; // radians: sets the strength (a 25° turn gives about 1)
  static const _knee = 0.785; // past about 45° the effect eases off, but never stops
  static const _limit = 1.4; // only a sanity bound (80°)
  static const _settle = 2.2; // seconds for the view to drift back to centre

  bool _enabled = true;
  int _users = 0;
  bool _front = true;
  int _quarterTurns = 0; // how the screen is turned: 0 portrait, 1 landscape (top left), 3 landscape (top right)
  bool _landscape = false;
  StreamSubscription<GyroscopeEvent>? _gyro;
  StreamSubscription<AccelerometerEvent>? _gravity;
  double _ax = 0, _ay = 0; // turned this far (radians), leaking back to 0
  Offset _smooth = Offset.zero;
  DateTime? _last;

  bool get enabled => _enabled;

  /// The setting in Settings → Look & feel.
  set enabled(bool on) {
    if (on == _enabled) return;
    _enabled = on;
    SharedPreferences.getInstance().then((p) => p.setBool(_pref, on)).catchError((_) => true);
    _update();
  }

  static bool get supported => Platform.isAndroid || Platform.isIOS;

  void attach() {
    _users++;
    _update();
  }

  void detach() {
    _users = max(0, _users - 1);
    _update();
  }

  /// Screens tell us when the phone is sideways, so "right" stays right on screen.
  set landscape(bool on) {
    if (on == _landscape) return;
    _landscape = on;
    _update();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _front = state == AppLifecycleState.resumed;
    _update();
  }

  void _update() {
    final run = supported && _enabled && _front && _users > 0;
    if (run && _gyro == null) {
      _last = null;
      _gyro = gyroscopeEventStream(samplingPeriod: SensorInterval.gameInterval).listen(_onTurn, onError: (Object _) => _stop());
    } else if (!run && _gyro != null) {
      _stop();
    }
    // sideways: gravity says which way round (checked slowly; it changes rarely)
    final watchGravity = run && _landscape;
    if (watchGravity && _gravity == null) {
      _gravity = accelerometerEventStream(samplingPeriod: SensorInterval.normalInterval).listen((e) {
        if (e.x.abs() > 5) _quarterTurns = e.x > 0 ? 1 : 3;
      }, onError: (Object _) {});
    } else if (!watchGravity) {
      _gravity?.cancel();
      _gravity = null;
      if (!_landscape) _quarterTurns = 0;
    }
  }

  void _stop() {
    _gyro?.cancel();
    _gyro = null;
    _ax = _ay = 0;
    _smooth = Offset.zero;
    value = Offset.zero;
  }

  void _onTurn(GyroscopeEvent e) {
    final now = DateTime.now();
    final dt = _last == null ? 0.02 : (now.difference(_last!).inMicroseconds / 1e6).clamp(0.0, 0.1);
    _last = now;
    // turning about the screen's up axis (x) and its right axis (y), whichever way the screen is round
    final (up, right) = switch (_quarterTurns) {
      1 => (e.x, -e.y),
      3 => (-e.x, e.y),
      _ => (e.y, e.x),
    };
    final leak = exp(-dt / _settle);
    _ax = ((_ax + up * dt) * leak).clamp(-_limit, _limit);
    _ay = ((_ay + right * dt) * leak).clamp(-_limit, _limit);
    // ease off gently toward the edges (no wall to hit), then smooth out sensor jitter
    final target = Offset(response(_ax), response(_ay));
    _smooth = Offset.lerp(_smooth, target, 1 - exp(-dt / 0.06))!;
    value = _smooth;
  }

  /// How much effect a turn of [radians] gives (about 1 for a 25° turn, up to about 1.7): close
  /// to linear through ordinary tilts, easing off past 45°, still moving at 80°. No wall to hit.
  static double response(double radians) {
    final k = radians / _knee;
    return radians / _unit / sqrt(1 + k * k);
  }
}

/// Keeps the tilt sensor running while this widget is on screen.
mixin TiltUser<T extends StatefulWidget> on State<T> {
  @override
  void initState() {
    super.initState();
    Tilt.instance.attach();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    Tilt.instance.landscape = MediaQuery.orientationOf(context) == Orientation.landscape;
  }

  @override
  void dispose() {
    Tilt.instance.detach();
    super.dispose();
  }
}
