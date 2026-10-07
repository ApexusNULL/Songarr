import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import '../theme.dart';
import 'tilt.dart';

/// The app's backdrop: soft aurora clouds drifting through deep space, with faint twinkling
/// stars. Drawn at up to 30 frames a second and paused when the app isn't visible or the
/// system asks for reduced motion. Turning the phone shifts the clouds and stars by their depth
/// (see [Tilt]), so the backdrop looks like deep space behind the screen.
class NebulaBackground extends StatefulWidget {
  const NebulaBackground({super.key, this.tint, this.intensity = 1.0});

  /// Shifts the clouds toward a colour, e.g. the album art's on Now Playing.
  final Color? tint;
  final double intensity;

  @override
  State<NebulaBackground> createState() => _NebulaBackgroundState();
}

class _NebulaBackgroundState extends State<NebulaBackground> with SingleTickerProviderStateMixin, WidgetsBindingObserver, TiltUser {
  late final Ticker _ticker = createTicker(_tick);
  final _time = ValueNotifier<double>(0);
  Duration _lastFrame = Duration.zero;
  double _offset = Random().nextDouble() * 100; // each launch starts somewhere new

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final still = MediaQuery.disableAnimationsOf(context);
    if (still && _ticker.isActive) _ticker.stop();
    if (!still && !_ticker.isActive) _ticker.start();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      if (!_ticker.isActive && !MediaQuery.disableAnimationsOf(context)) _ticker.start();
    } else if (_ticker.isActive) {
      _offset = _time.value;
      _lastFrame = Duration.zero;
      _ticker.stop();
    }
  }

  void _tick(Duration elapsed) {
    if (elapsed - _lastFrame < const Duration(milliseconds: 33)) return;
    _lastFrame = elapsed;
    _time.value = _offset + elapsed.inMicroseconds / 1e6;
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _ticker.dispose();
    _time.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => RepaintBoundary(
        child: CustomPaint(painter: _NebulaPainter(_time, widget.tint, widget.intensity), size: Size.infinite),
      );
}

class _Star {
  _Star(Random r)
      : x = r.nextDouble(),
        y = r.nextDouble(),
        size = 0.4 + pow(r.nextDouble(), 3) * 1.6,
        drift = 0.2 + r.nextDouble() * 0.8,
        twinkle = 0.6 + r.nextDouble() * 1.8,
        phase = r.nextDouble() * pi * 2,
        depth = r.nextDouble();
  final double x, y, size, drift, twinkle, phase;

  /// 0 nearest .. 1 farthest; the big bright stars are the near ones.
  final double depth;
  double get distance => (0.25 + 0.75 * depth) * (1.15 - 0.45 * (size - 0.4) / 1.6);
}

/// How far (logical pixels) the farthest stars and clouds shift at full tilt.
const _starShift = 36.0;
const _cloudShift = 26.0;

final _stars = List.generate(90, (i) => _Star(Random(i * 7919 + 13)));

class _NebulaPainter extends CustomPainter {
  _NebulaPainter(this.time, this.tint, this.intensity) : super(repaint: time);
  final ValueNotifier<double> time;
  final Color? tint;
  final double intensity;

  @override
  void paint(Canvas canvas, Size size) {
    final t = time.value;
    final tilt = Tilt.instance.value; // things behind the screen shift against the turn
    final full = Offset.zero & size;
    canvas.drawRect(full, Paint()..color = background);
    final colors = tint == null
        ? const [accent, accent2, Color(0xFF0EA5E9), Color(0xFF6D28D9)]
        : [tint!, Color.lerp(tint, accent2, 0.45)!, Color.lerp(tint, accent, 0.5)!, accent];
    // (x, y, wander x, wander y, speed, phase, radius, strength, depth)
    const blobs = [
      (0.15, 0.10, 0.20, 0.12, 0.050, 0.0, 0.75, 0.30, 1.0),
      (0.90, 0.30, 0.15, 0.20, 0.037, 2.1, 0.70, 0.22, 0.7),
      (0.30, 0.85, 0.25, 0.10, 0.043, 4.2, 0.80, 0.16, 0.85),
      (0.75, 0.75, 0.18, 0.18, 0.029, 1.3, 0.65, 0.18, 0.6),
    ];
    for (var i = 0; i < blobs.length; i++) {
      final (bx, by, wx, wy, speed, phase, r, strength, depth) = blobs[i];
      final c = Offset(size.width * (bx + wx * sin(t * speed + phase)), size.height * (by + wy * cos(t * speed * 0.8 + phase))) -
          tilt * (_cloudShift * depth);
      final radius = size.longestSide * r;
      final color = colors[i % colors.length];
      final a = (strength * intensity * (0.85 + 0.15 * sin(t * speed * 3 + phase))).clamp(0.0, 1.0);
      canvas.drawRect(
        full,
        Paint()
          ..shader = RadialGradient(colors: [color.withValues(alpha: a), color.withValues(alpha: a * 0.35), color.withValues(alpha: 0)],
                  stops: const [0, 0.45, 1])
              .createShader(Rect.fromCircle(center: c, radius: radius)),
      );
    }
    final star = Paint();
    for (final s in _stars) {
      final shift = tilt * (_starShift * s.distance);
      final x = (s.x - shift.dx / size.width) % 1.0;
      final y = (s.y - t * s.drift * 0.004 - shift.dy / size.height) % 1.0;
      final alpha = (0.12 + 0.55 * (0.5 + 0.5 * sin(t * s.twinkle + s.phase))) * intensity.clamp(0.0, 1.0);
      final p = Offset(x * size.width, y * size.height);
      star.color = Colors.white.withValues(alpha: alpha);
      canvas.drawCircle(p, s.size, star);
      if (s.size > 1.4) {
        // the brightest stars get a faint cross glint
        star
          ..color = Colors.white.withValues(alpha: alpha * 0.35)
          ..strokeWidth = 0.6;
        canvas.drawLine(p.translate(-s.size * 3, 0), p.translate(s.size * 3, 0), star);
        canvas.drawLine(p.translate(0, -s.size * 3), p.translate(0, s.size * 3), star);
      }
    }
    // keep text readable toward the bottom, where lists end and the player sits
    canvas.drawRect(
      full,
      Paint()
        ..shader = LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [Colors.transparent, background.withValues(alpha: 0.55)],
          stops: const [0.45, 1],
        ).createShader(full),
    );
  }

  @override
  bool shouldRepaint(_NebulaPainter old) => old.tint != tint || old.intensity != intensity;
}
