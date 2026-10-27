import 'dart:async';
import 'dart:io';
import 'dart:math';

import 'package:flutter/gestures.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';

/// The one clock all the decorative motion runs on: the drifting nebula, sparkles, the turning
/// disc, the dancing bars. Each of them used to redraw the whole screen at the display's own rate
/// (60 to 120 times a second, glass blurs and all) for as long as it was on screen, which made
/// the app a heavy drain on a phone's battery even with nothing playing. This ticks [fps] times a
/// second instead, one redraw for all of them, and only:
/// - while something on screen uses it ([FxUser]: not on a hidden tab or a page underneath),
/// - while the app is in front,
/// - for [awake] after the last touch or scroll: then everything rests, still, until the next
///   touch (a phone left showing Now Playing costs next to nothing),
/// - and never while the phone is saving battery (Battery Saver) or asks for reduced motion.
class FxClock extends ChangeNotifier with WidgetsBindingObserver {
  FxClock._() {
    WidgetsBinding.instance.addObserver(this);
    GestureBinding.instance.pointerRouter.addGlobalRoute(_onPointer);
    _checkSaver();
  }

  static final instance = FxClock._();
  static const fps = 20;
  static const awake = Duration(seconds: 20);
  static const _power = MethodChannel('songarr/power');

  /// Seconds of motion so far. It stands still while resting, so nothing jumps when it wakes.
  double get seconds => _seconds;
  double _seconds = Random().nextDouble() * 100; // each launch starts somewhere new

  /// Whether it's ticking right now (the tilt sensor only runs while it is).
  final running = ValueNotifier<bool>(false);

  int _users = 0;
  bool _front = true;
  bool _saver = false;
  DateTime _touched = DateTime.now();
  DateTime _last = DateTime.now();
  Timer? _timer;

  void attach() {
    _users++;
    _update();
  }

  void detach() {
    _users = max(0, _users - 1);
    _update();
  }

  /// Motion again for a while (a touch does this by itself).
  void wake() {
    _touched = DateTime.now();
    if (_timer == null) _update();
  }

  void _onPointer(PointerEvent e) {
    if (e is PointerDownEvent || e is PointerMoveEvent || e is PointerSignalEvent || e is PointerPanZoomUpdateEvent) wake();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _front = state == AppLifecycleState.resumed;
    if (_front) {
      _touched = DateTime.now();
      _checkSaver(); // (Battery Saver is usually switched from the quick settings, away from the app)
    }
    _update();
  }

  Future<void> _checkSaver() async {
    if (!Platform.isAndroid) return;
    try {
      _saver = await _power.invokeMethod<bool>('saving') ?? false;
    } on Exception {
      _saver = false;
    }
    _update();
  }

  bool get _wanted => _users > 0 && _front && !_saver && DateTime.now().difference(_touched) < awake;

  void _update() {
    final run = _wanted;
    if (run && _timer == null) {
      _last = DateTime.now();
      _timer = Timer.periodic(const Duration(microseconds: 1000000 ~/ fps), _tick);
    } else if (!run && _timer != null) {
      _timer!.cancel();
      _timer = null;
    }
    running.value = _timer != null;
  }

  void _tick(Timer _) {
    final now = DateTime.now();
    _seconds += min(now.difference(_last).inMicroseconds / 1e6, 0.25);
    _last = now;
    notifyListeners();
    if (!_wanted) _update(); // rest (or stop) after this last frame
  }
}

/// For a widget that moves with [FxClock]: it holds the clock while it's on screen (not on a
/// hidden tab, not under another page, not while the phone asks for reduced motion) and while
/// [wantsClock] says so (the disc turns only while the music plays, say).
mixin FxUser<T extends StatefulWidget> on State<T> {
  bool _holding = false;

  /// Whether this has the clock now (on screen and wanting it): only then should a tick move it.
  bool get holdingClock => _holding;

  /// Whether this needs the clock now. Call [syncClock] when it changes.
  bool get wantsClock => true;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    syncClock();
  }

  void syncClock() {
    final want = wantsClock && TickerMode.valuesOf(context).enabled && !MediaQuery.disableAnimationsOf(context);
    if (want == _holding) return;
    _holding = want;
    want ? FxClock.instance.attach() : FxClock.instance.detach();
  }

  @override
  void dispose() {
    if (_holding) FxClock.instance.detach();
    super.dispose();
  }
}

/// Whether [context]'s screen is what's showing: the app in front, and not a hidden tab or a page
/// under another one. For "look again every so often" timers, which needn't run unseen.
bool showing(BuildContext context) {
  final s = WidgetsBinding.instance.lifecycleState;
  final front = s == null || s == AppLifecycleState.resumed || s == AppLifecycleState.inactive;
  return front && context.mounted && TickerMode.getValuesNotifier(context).value.enabled;
}
