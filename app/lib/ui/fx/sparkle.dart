import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import '../theme.dart';
import 'tilt.dart';

/// A tiny particle system for sparkles: glowing dots and four-pointed glints that drift,
/// twinkle and fade.
class Particle {
  Particle(this.pos, this.vel, this.life, this.size, this.color, this.glint, this.phase) : maxLife = life;
  Offset pos;
  Offset vel;
  double life;
  final double maxLife;
  final double size;
  final Color color;
  final bool glint;
  final double phase;
}

class ParticleField {
  final particles = <Particle>[];
  final rnd = Random();

  bool get isEmpty => particles.isEmpty;

  void spawn(Offset at, {double minSpeed = 6, double maxSpeed = 40, double angle = -pi / 2, double spread = pi, double minLife = 0.6,
      double maxLife = 1.3, double minSize = 1.0, double maxSize = 2.8, List<Color> colors = sparkleColors, double glints = 0.45}) {
    final a = angle + (rnd.nextDouble() - 0.5) * spread;
    final speed = minSpeed + rnd.nextDouble() * (maxSpeed - minSpeed);
    particles.add(Particle(
      at,
      Offset(cos(a), sin(a)) * speed,
      minLife + rnd.nextDouble() * (maxLife - minLife),
      minSize + rnd.nextDouble() * (maxSize - minSize),
      colors[rnd.nextInt(colors.length)],
      rnd.nextDouble() < glints,
      rnd.nextDouble() * pi * 2,
    ));
  }

  void step(double dt, {double gravity = 0, double drag = 1.2}) {
    final damp = exp(-drag * dt);
    for (final p in particles) {
      p.vel = Offset(p.vel.dx * damp, p.vel.dy * damp + gravity * dt);
      p.pos += p.vel * dt;
      p.life -= dt;
    }
    particles.removeWhere((p) => p.life <= 0);
  }

  /// [parallax]: how far the biggest (nearest) particles are shifted; smaller ones move less.
  void paint(Canvas canvas, {double time = 0, Offset parallax = Offset.zero, double maxSize = 2.8}) {
    final glow = Paint()..maskFilter = const MaskFilter.blur(BlurStyle.normal, 3);
    final solid = Paint();
    for (final p in particles) {
      final at = parallax == Offset.zero ? p.pos : p.pos + parallax * (0.35 + 0.65 * (p.size / maxSize).clamp(0.0, 1.0));
      final k = (p.life / p.maxLife).clamp(0.0, 1.0);
      final twinkle = 0.7 + 0.3 * sin(time * 14 + p.phase);
      final a = pow(k, 0.7) * twinkle;
      final r = p.size * (0.5 + 0.5 * k);
      glow.color = p.color.withValues(alpha: (a * 0.55).toDouble());
      canvas.drawCircle(at, r * 2.2, glow);
      solid.color = p.color.withValues(alpha: a.toDouble());
      if (p.glint) {
        canvas.drawPath(glintPath(at, r * 2.4), solid);
      } else {
        canvas.drawCircle(at, r * 0.8, solid);
      }
    }
  }
}

/// A concave four-pointed star.
Path glintPath(Offset c, double r) => Path()
  ..moveTo(c.dx, c.dy - r)
  ..quadraticBezierTo(c.dx, c.dy, c.dx + r, c.dy)
  ..quadraticBezierTo(c.dx, c.dy, c.dx, c.dy + r)
  ..quadraticBezierTo(c.dx, c.dy, c.dx - r, c.dy)
  ..quadraticBezierTo(c.dx, c.dy, c.dx, c.dy - r)
  ..close();

/// The playback bar: an aurora-gradient line whose glowing head sheds a few sparkles while
/// music plays. Drag or tap to seek; a time bubble follows your finger.
class SparkleBar extends StatefulWidget {
  const SparkleBar({
    super.key,
    required this.position,
    required this.duration,
    this.playing = false,
    this.speed = 1.0,
    this.onSeek,
    this.height = 36,
    this.trackHeight = 4,
    this.sparklesPerSecond = 12,
    this.showThumb = true,
  });

  final Duration position;
  final Duration duration;
  final bool playing;
  final double speed;
  final ValueChanged<Duration>? onSeek;
  final double height;
  final double trackHeight;
  final double sparklesPerSecond;
  final bool showThumb;

  @override
  State<SparkleBar> createState() => _SparkleBarState();
}

class _SparkleBarState extends State<SparkleBar> with SingleTickerProviderStateMixin {
  late final Ticker _ticker = createTicker(_tick);
  final _field = ParticleField();
  final _repaint = ValueNotifier<int>(0);
  final _clock = Stopwatch()..start();
  Duration _lastTick = Duration.zero;
  double _debt = 0;
  double _time = 0;
  Size _size = Size.zero;
  double? _drag; // 0..1 while the finger is down

  // The player reports position a few times a second; in between, the head glides on.
  Duration _base = Duration.zero;
  int _baseAt = 0;

  bool get _still => MediaQuery.disableAnimationsOf(context);

  @override
  void initState() {
    super.initState();
    _rebase();
  }

  void _rebase() {
    _base = widget.position;
    _baseAt = _clock.elapsedMicroseconds;
  }

  double get fraction {
    if (_drag != null) return _drag!;
    final total = widget.duration.inMicroseconds;
    if (total <= 0) return 0;
    var pos = _base.inMicroseconds;
    if (widget.playing) pos += ((_clock.elapsedMicroseconds - _baseAt) * widget.speed).round();
    return (pos / total).clamp(0.0, 1.0);
  }

  @override
  void didUpdateWidget(SparkleBar old) {
    super.didUpdateWidget(old);
    _rebase();
    _wake();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _wake();
  }

  void _wake() {
    if ((widget.playing && !_still || _field.particles.isNotEmpty || _drag != null) && !_ticker.isActive) {
      _lastTick = Duration.zero;
      _ticker.start();
    }
  }

  Offset get _head => Offset(fraction * _size.width, _size.height / 2);

  void _tick(Duration elapsed) {
    final dt = _lastTick == Duration.zero ? 1 / 60 : ((elapsed - _lastTick).inMicroseconds / 1e6).clamp(0.0, 0.05);
    _lastTick = elapsed;
    _time += dt;
    if (widget.playing && !_still && _size.width > 0) {
      _debt += dt * widget.sparklesPerSecond;
      while (_debt >= 1) {
        _debt -= 1;
        _field.spawn(_head + Offset(0, (_field.rnd.nextDouble() - 0.5) * widget.trackHeight),
            angle: -pi * 0.62, spread: pi * 0.9, minSpeed: 8, maxSpeed: 34, minSize: widget.trackHeight * 0.25,
            maxSize: widget.trackHeight * 0.7);
      }
    }
    _field.step(dt, gravity: -10, drag: 1.6);
    _repaint.value++;
    if (!widget.playing && _field.isEmpty && _drag == null) _ticker.stop();
  }

  void _burst() {
    if (_still) return;
    for (var i = 0; i < 16; i++) {
      _field.spawn(_head, angle: -pi / 2, spread: pi * 2, minSpeed: 30, maxSpeed: 90, minLife: 0.4, maxLife: 0.9);
    }
    _wake();
  }

  double _fractionAt(Offset local) => _size.width <= 0 ? 0 : (local.dx / _size.width).clamp(0.0, 1.0);

  void _seekTo(double f) {
    final to = Duration(microseconds: (widget.duration.inMicroseconds * f).round());
    _base = to;
    _baseAt = _clock.elapsedMicroseconds;
    widget.onSeek?.call(to);
  }

  @override
  void dispose() {
    _ticker.dispose();
    _repaint.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final interactive = widget.onSeek != null;
    Widget bar = LayoutBuilder(builder: (context, box) {
      _size = Size(box.maxWidth, widget.height);
      return CustomPaint(size: _size, painter: _SparkleBarPainter(this, _repaint));
    });
    if (interactive) {
      bar = GestureDetector(
        behavior: HitTestBehavior.opaque,
        onHorizontalDragStart: (d) => setState(() {
          _drag = _fractionAt(d.localPosition);
          _wake();
        }),
        onHorizontalDragUpdate: (d) => setState(() => _drag = _fractionAt(d.localPosition)),
        onHorizontalDragEnd: (_) {
          final f = _drag ?? fraction;
          setState(() => _drag = null);
          _seekTo(f);
          _burst();
        },
        onHorizontalDragCancel: () => setState(() => _drag = null),
        onTapUp: (d) {
          _seekTo(_fractionAt(d.localPosition));
          _burst();
        },
        child: bar,
      );
    }
    return SizedBox(height: widget.height, child: bar);
  }
}

class _SparkleBarPainter extends CustomPainter {
  _SparkleBarPainter(this.s, Listenable repaint) : super(repaint: repaint);
  final _SparkleBarState s;

  @override
  void paint(Canvas canvas, Size size) {
    final w = s.widget;
    final cy = size.height / 2;
    final th = w.trackHeight;
    final f = s.fraction;
    final head = Offset(f * size.width, cy);
    final track = RRect.fromLTRBR(0, cy - th / 2, size.width, cy + th / 2, Radius.circular(th));
    canvas.drawRRect(track, Paint()..color = Colors.white.withValues(alpha: 0.13));
    if (f > 0) {
      final done = RRect.fromLTRBR(0, cy - th / 2, max(th, head.dx), cy + th / 2, Radius.circular(th));
      final shader = aurora.createShader(Offset.zero & size);
      canvas.drawRRect(done.inflate(th * 0.6), Paint()
        ..shader = shader
        ..maskFilter = MaskFilter.blur(BlurStyle.normal, th * 1.4)
        ..color = Colors.white.withValues(alpha: 0.5));
      canvas.drawRRect(done, Paint()..shader = shader);
    }
    s._field.paint(canvas, time: s._time);
    if (w.showThumb) {
      final dragging = s._drag != null;
      final r = th * (dragging ? 2.4 : 1.6);
      canvas.drawCircle(head, r * 2.2, Paint()
        ..color = glowColor.withValues(alpha: 0.55)
        ..maskFilter = MaskFilter.blur(BlurStyle.normal, r * 1.4));
      canvas.drawCircle(head, r, Paint()..color = Colors.white);
      if (dragging) {
        final at = Duration(microseconds: (w.duration.inMicroseconds * f).round());
        final tp = TextPainter(
          text: TextSpan(text: _fmt(at), style: const TextStyle(color: Colors.white, fontSize: 12, fontWeight: FontWeight.w700)),
          textDirection: TextDirection.ltr,
        )..layout();
        final bubble = RRect.fromRectAndRadius(
          Rect.fromCenter(center: head.translate(0, -26), width: tp.width + 16, height: tp.height + 8),
          const Radius.circular(10),
        );
        canvas.drawRRect(bubble, Paint()..color = surfaceHigh);
        canvas.drawRRect(bubble, Paint()
          ..style = PaintingStyle.stroke
          ..color = glassBorder);
        tp.paint(canvas, bubble.center - Offset(tp.width / 2, tp.height / 2));
      }
    }
  }

  static String _fmt(Duration d) {
    final h = d.inHours, m = d.inMinutes % 60, sec = d.inSeconds % 60;
    return h > 0 ? '$h:${m.toString().padLeft(2, '0')}:${sec.toString().padLeft(2, '0')}' : '$m:${sec.toString().padLeft(2, '0')}';
  }

  @override
  bool shouldRepaint(_SparkleBarPainter old) => true;
}

/// Slowly rising, twinkling motes for headers and empty states. They float in front of the
/// screen: turning the phone shifts them with the turn (see [Tilt]).
class AmbientSparkles extends StatefulWidget {
  const AmbientSparkles({super.key, this.perSecond = 6, this.colors = sparkleColors, this.maxSize = 2.4});
  final double perSecond;
  final List<Color> colors;
  final double maxSize;

  @override
  State<AmbientSparkles> createState() => _AmbientSparklesState();
}

class _AmbientSparklesState extends State<AmbientSparkles> with SingleTickerProviderStateMixin, TiltUser {
  late final Ticker _ticker = createTicker(_tick);
  final _field = ParticleField();
  final _repaint = ValueNotifier<int>(0);
  Size _size = Size.zero;
  Duration _last = Duration.zero;
  double _debt = 0;
  double _time = 0;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final still = MediaQuery.disableAnimationsOf(context);
    if (still && _ticker.isActive) _ticker.stop();
    if (!still && !_ticker.isActive) _ticker.start();
  }

  void _tick(Duration elapsed) {
    final dt = _last == Duration.zero ? 1 / 60 : ((elapsed - _last).inMicroseconds / 1e6).clamp(0.0, 0.05);
    _last = elapsed;
    _time += dt;
    if (_size.width > 0) {
      _debt += dt * widget.perSecond;
      while (_debt >= 1) {
        _debt -= 1;
        final r = _field.rnd;
        _field.spawn(Offset(r.nextDouble() * _size.width, _size.height * (0.35 + r.nextDouble() * 0.65)),
            angle: -pi / 2, spread: 0.6, minSpeed: 6, maxSpeed: 22, minLife: 1.6, maxLife: 3.2, minSize: 0.8,
            maxSize: widget.maxSize, colors: widget.colors, glints: 0.35);
      }
    }
    _field.step(dt, drag: 0.2);
    _repaint.value++;
  }

  @override
  void dispose() {
    _ticker.dispose();
    _repaint.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => IgnorePointer(
        child: RepaintBoundary(
          child: LayoutBuilder(builder: (context, box) {
            _size = box.biggest;
            return CustomPaint(size: _size, painter: _FieldPainter(_field, _repaint, () => _time, widget.maxSize));
          }),
        ),
      );
}

class _FieldPainter extends CustomPainter {
  _FieldPainter(this.field, Listenable repaint, this.time, this.maxSize) : super(repaint: repaint);
  final ParticleField field;
  final double Function() time;
  final double maxSize;

  /// How far the nearest sparkles shift at full tilt (with the turn: they're in front of the screen).
  static const _shift = 16.0;

  @override
  void paint(Canvas canvas, Size size) =>
      field.paint(canvas, time: time(), parallax: Tilt.instance.value * _shift, maxSize: maxSize);

  @override
  bool shouldRepaint(_FieldPainter old) => true;
}

/// The like button: a heart that pops and bursts into sparkles when you like a song.
class LikeButton extends StatefulWidget {
  const LikeButton({super.key, required this.liked, required this.onPressed, this.size = 28});
  final bool liked;
  final VoidCallback onPressed;
  final double size;

  @override
  State<LikeButton> createState() => _LikeButtonState();
}

class _LikeButtonState extends State<LikeButton> with TickerProviderStateMixin {
  late final _pop = AnimationController(vsync: this, duration: const Duration(milliseconds: 520));
  late final Ticker _ticker = createTicker(_tick);
  final _field = ParticleField();
  final _repaint = ValueNotifier<int>(0);
  Duration _last = Duration.zero;
  double _time = 0;

  static final _scale = TweenSequence([
    TweenSequenceItem(tween: Tween(begin: 1.0, end: 1.4).chain(CurveTween(curve: Curves.easeOut)), weight: 35),
    TweenSequenceItem(tween: Tween(begin: 1.4, end: 0.88).chain(CurveTween(curve: Curves.easeInOut)), weight: 25),
    TweenSequenceItem(tween: Tween(begin: 0.88, end: 1.0).chain(CurveTween(curve: Curves.elasticOut)), weight: 40),
  ]);

  void _tick(Duration elapsed) {
    final dt = _last == Duration.zero ? 1 / 60 : ((elapsed - _last).inMicroseconds / 1e6).clamp(0.0, 0.05);
    _last = elapsed;
    _time += dt;
    _field.step(dt, gravity: 70, drag: 2.2);
    _repaint.value++;
    if (_field.isEmpty) {
      _ticker.stop();
      _last = Duration.zero;
    }
  }

  void _press() {
    if (!widget.liked && !MediaQuery.disableAnimationsOf(context)) {
      _pop.forward(from: 0);
      final c = Offset(widget.size, widget.size); // centre of the 2x-size paint area
      for (var i = 0; i < 18; i++) {
        _field.spawn(c, angle: 0, spread: pi * 2, minSpeed: 70, maxSpeed: 170, minLife: 0.45, maxLife: 0.9, minSize: 1.2,
            maxSize: 3.0, colors: const [accent2, accent, glowColor, accent3, Colors.white], glints: 0.6);
      }
      if (!_ticker.isActive) _ticker.start();
    }
    widget.onPressed();
  }

  @override
  void dispose() {
    _pop.dispose();
    _ticker.dispose();
    _repaint.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.size;
    final heart = widget.liked
        ? ShaderMask(
            blendMode: BlendMode.srcIn,
            shaderCallback: (r) => const LinearGradient(colors: [accent2, accent], begin: Alignment.topLeft, end: Alignment.bottomRight)
                .createShader(r),
            child: Icon(Icons.favorite_rounded, size: s),
          )
        : Icon(Icons.favorite_border_rounded, size: s, color: Colors.white70);
    return Semantics(
      button: true,
      label: widget.liked ? 'Remove from Liked Songs' : 'Add to Liked Songs',
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTap: _press,
        child: SizedBox(
          width: s * 1.7,
          height: s * 1.7,
          child: Stack(clipBehavior: Clip.none, alignment: Alignment.center, children: [
            Positioned(
              left: s * 0.85 - s,
              top: s * 0.85 - s,
              width: s * 2,
              height: s * 2,
              child: IgnorePointer(
                child: AnimatedBuilder(
                  animation: _pop,
                  builder: (context, _) => CustomPaint(painter: _BurstPainter(_field, _repaint, _pop.value, () => _time)),
                ),
              ),
            ),
            ScaleTransition(scale: _scale.animate(_pop), child: heart),
          ]),
        ),
      ),
    );
  }
}

class _BurstPainter extends CustomPainter {
  _BurstPainter(this.field, Listenable repaint, this.ring, this.time) : super(repaint: repaint);
  final ParticleField field;
  final double ring;
  final double Function() time;

  @override
  void paint(Canvas canvas, Size size) {
    if (ring > 0 && ring < 1) {
      final c = size.center(Offset.zero);
      canvas.drawCircle(c, size.width * (0.25 + 0.45 * Curves.easeOut.transform(ring)), Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2.5 * (1 - ring)
        ..color = accent2.withValues(alpha: 0.8 * (1 - ring)));
    }
    field.paint(canvas, time: time());
  }

  @override
  bool shouldRepaint(_BurstPainter old) => true;
}
