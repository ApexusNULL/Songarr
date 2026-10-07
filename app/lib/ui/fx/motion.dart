import 'dart:math';
import 'dart:ui' show ImageFilter;

import 'package:flutter/material.dart';

import '../theme.dart';

/// Frosted glass: blurs whatever is behind it (use blur sparingly: it costs a little per frame).
class Glass extends StatelessWidget {
  const Glass({super.key, required this.child, this.radius = radiusL, this.blur = true, this.padding, this.tint});
  final Widget child;
  final double radius;
  final bool blur;
  final EdgeInsetsGeometry? padding;
  final Color? tint;

  @override
  Widget build(BuildContext context) {
    final r = BorderRadius.circular(radius);
    final body = DecoratedBox(
      decoration: BoxDecoration(
        borderRadius: r,
        border: Border.all(color: glassBorder),
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            (tint ?? Colors.white).withValues(alpha: blur ? 0.10 : 0.08),
            (tint ?? Colors.white).withValues(alpha: blur ? 0.04 : 0.03),
          ],
        ),
      ),
      child: padding == null ? child : Padding(padding: padding!, child: child),
    );
    return ClipRRect(
      borderRadius: r,
      child: blur ? BackdropFilter(filter: ImageFilter.blur(sigmaX: 22, sigmaY: 22), child: body) : body,
    );
  }
}

/// Text or icons painted with the aurora gradient.
class GradientMask extends StatelessWidget {
  const GradientMask({super.key, required this.child, this.gradient = aurora});
  final Widget child;
  final Gradient gradient;

  @override
  Widget build(BuildContext context) => ShaderMask(
        blendMode: BlendMode.srcIn,
        shaderCallback: (bounds) => gradient.createShader(Offset.zero & bounds.size),
        child: child,
      );
}

/// Squishes a little under your finger and springs back.
class Pressable extends StatefulWidget {
  const Pressable({super.key, required this.child, this.onTap, this.onLongPress, this.scale = 0.95});
  final Widget child;
  final VoidCallback? onTap;
  final VoidCallback? onLongPress;
  final double scale;

  @override
  State<Pressable> createState() => _PressableState();
}

class _PressableState extends State<Pressable> with SingleTickerProviderStateMixin {
  late final _c = AnimationController(vsync: this, duration: const Duration(milliseconds: 110), reverseDuration: const Duration(milliseconds: 380));
  late final _scale = Tween(begin: 1.0, end: widget.scale)
      .animate(CurvedAnimation(parent: _c, curve: Curves.easeOut, reverseCurve: Curves.elasticOut));

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final enabled = widget.onTap != null || widget.onLongPress != null;
    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTapDown: enabled ? (_) => _c.forward() : null,
      onTapUp: enabled ? (_) => _c.reverse() : null,
      onTapCancel: enabled ? () => _c.reverse() : null,
      onTap: widget.onTap,
      onLongPress: widget.onLongPress == null
          ? null
          : () {
              _c.reverse();
              widget.onLongPress!();
            },
      child: ScaleTransition(scale: _scale, child: widget.child),
    );
  }
}

/// Fades and floats into place; [index] staggers items in a list.
class FadeSlideIn extends StatefulWidget {
  const FadeSlideIn({super.key, required this.child, this.index = 0, this.offset = 18, this.axis = Axis.vertical});
  final Widget child;
  final int index;
  final double offset;
  final Axis axis;

  @override
  State<FadeSlideIn> createState() => _FadeSlideInState();
}

class _FadeSlideInState extends State<FadeSlideIn> with SingleTickerProviderStateMixin {
  late final _c = AnimationController(vsync: this, duration: const Duration(milliseconds: 460));
  late final _curve = CurvedAnimation(parent: _c, curve: Curves.easeOutCubic);
  bool _started = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_started) return;
    _started = true;
    if (MediaQuery.disableAnimationsOf(context)) {
      _c.value = 1;
      return;
    }
    final delay = Duration(milliseconds: 45 * min(widget.index, 10));
    Future.delayed(delay, () {
      if (mounted) _c.forward();
    });
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
        animation: _curve,
        child: widget.child,
        builder: (context, child) {
          final d = (1 - _curve.value) * widget.offset;
          return Opacity(
            opacity: _curve.value,
            child: Transform.translate(offset: widget.axis == Axis.vertical ? Offset(0, d) : Offset(d, 0), child: child),
          );
        },
      );
}

/// Dancing bars next to the song that's playing.
class EqualizerBars extends StatefulWidget {
  const EqualizerBars({super.key, required this.playing, this.size = 16});
  final bool playing;
  final double size;

  @override
  State<EqualizerBars> createState() => _EqualizerBarsState();
}

class _EqualizerBarsState extends State<EqualizerBars> with SingleTickerProviderStateMixin {
  late final _c = AnimationController(vsync: this, duration: const Duration(milliseconds: 1400));

  @override
  void initState() {
    super.initState();
    if (widget.playing) _c.repeat();
  }

  @override
  void didUpdateWidget(EqualizerBars old) {
    super.didUpdateWidget(old);
    if (widget.playing && !_c.isAnimating) _c.repeat();
    if (!widget.playing && _c.isAnimating) _c.stop();
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => GradientMask(
        child: AnimatedBuilder(
          animation: _c,
          builder: (context, _) => CustomPaint(size: Size.square(widget.size), painter: _BarsPainter(_c.value, widget.playing)),
        ),
      );
}

class _BarsPainter extends CustomPainter {
  _BarsPainter(this.t, this.playing);
  final double t;
  final bool playing;

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width / 5;
    final paint = Paint()..color = Colors.white;
    for (var i = 0; i < 3; i++) {
      final h = playing ? 0.3 + 0.7 * (0.5 + 0.5 * sin(t * pi * 2 * (1 + i * 0.37) + i * 1.9)).abs() : 0.3;
      final x = i * w * 2;
      canvas.drawRRect(
        RRect.fromLTRBR(x, size.height * (1 - h), x + w, size.height, Radius.circular(w / 2)),
        paint,
      );
    }
  }

  @override
  bool shouldRepaint(_BarsPainter old) => old.t != t || old.playing != playing;
}

/// A glowing placeholder while something loads.
class ShimmerBox extends StatefulWidget {
  const ShimmerBox({super.key, this.width, this.height, this.radius = radiusM, this.circle = false});
  final double? width;
  final double? height;
  final double radius;
  final bool circle;

  @override
  State<ShimmerBox> createState() => _ShimmerBoxState();
}

class _ShimmerBoxState extends State<ShimmerBox> with SingleTickerProviderStateMixin {
  late final _c = AnimationController(vsync: this, duration: const Duration(milliseconds: 1500))..repeat();

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
        animation: _c,
        builder: (context, _) {
          final x = -1.5 + _c.value * 3;
          return Container(
            width: widget.width,
            height: widget.height,
            decoration: BoxDecoration(
              shape: widget.circle ? BoxShape.circle : BoxShape.rectangle,
              borderRadius: widget.circle ? null : BorderRadius.circular(widget.radius),
              gradient: LinearGradient(
                begin: Alignment(x - 1, -0.3),
                end: Alignment(x + 1, 0.3),
                colors: const [Color(0x10FFFFFF), Color(0x24C4B5FD), Color(0x10FFFFFF)],
              ),
            ),
          );
        },
      );
}

/// The round aurora play/pause button; the icon morphs between the two.
class AuroraPlayButton extends StatefulWidget {
  const AuroraPlayButton({super.key, required this.playing, required this.onPressed, this.size = 72, this.loading = false});
  final bool playing;
  final bool loading;
  final VoidCallback? onPressed;
  final double size;

  @override
  State<AuroraPlayButton> createState() => _AuroraPlayButtonState();
}

class _AuroraPlayButtonState extends State<AuroraPlayButton> with SingleTickerProviderStateMixin {
  late final _c = AnimationController(vsync: this, duration: const Duration(milliseconds: 300), value: widget.playing ? 1 : 0);

  @override
  void didUpdateWidget(AuroraPlayButton old) {
    super.didUpdateWidget(old);
    if (widget.playing != old.playing) widget.playing ? _c.forward() : _c.reverse();
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.size;
    return Semantics(
      button: true,
      label: widget.playing ? 'Pause' : 'Play',
      child: Pressable(
        onTap: widget.onPressed,
        scale: 0.9,
        child: Container(
          width: s,
          height: s,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            gradient: const LinearGradient(colors: [accent, accent2, accent3], begin: Alignment.topLeft, end: Alignment.bottomRight),
            boxShadow: [
              BoxShadow(color: accent2.withValues(alpha: 0.45), blurRadius: s * 0.4, spreadRadius: 1),
              BoxShadow(color: accent.withValues(alpha: 0.35), blurRadius: s * 0.8, spreadRadius: 2),
            ],
          ),
          child: Stack(alignment: Alignment.center, children: [
            AnimatedIcon(icon: AnimatedIcons.play_pause, progress: _c, size: s * 0.5, color: Colors.white),
            if (widget.loading)
              SizedBox(width: s - 6, height: s - 6, child: const CircularProgressIndicator(strokeWidth: 2.5, color: Colors.white70)),
          ]),
        ),
      ),
    );
  }
}

/// A small round glass button for secondary actions.
class GlassIconButton extends StatelessWidget {
  const GlassIconButton({super.key, required this.icon, required this.onPressed, this.tooltip, this.size = 42, this.color});
  final IconData icon;
  final VoidCallback? onPressed;
  final String? tooltip;
  final double size;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final button = Pressable(
      onTap: onPressed,
      scale: 0.88,
      child: Glass(
        radius: size / 2,
        blur: false,
        child: SizedBox(width: size, height: size, child: Icon(icon, size: size * 0.5, color: color ?? Colors.white)),
      ),
    );
    return tooltip == null ? button : Tooltip(message: tooltip!, child: button);
  }
}

/// A section title with an optional "see all" style action.
class SectionTitle extends StatelessWidget {
  const SectionTitle(this.title, {super.key, this.subtitle, this.action, this.onAction, this.padding});
  final String title;
  final String? subtitle;
  final String? action;
  final VoidCallback? onAction;
  final EdgeInsetsGeometry? padding;

  @override
  Widget build(BuildContext context) => Padding(
        padding: padding ?? const EdgeInsets.fromLTRB(20, 26, 12, 12),
        child: Row(crossAxisAlignment: CrossAxisAlignment.end, children: [
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(title, style: Theme.of(context).textTheme.titleLarge),
              if (subtitle != null)
                Padding(padding: const EdgeInsets.only(top: 2), child: Text(subtitle!, style: const TextStyle(color: muted, fontSize: 13))),
            ]),
          ),
          if (action != null) TextButton(onPressed: onAction, child: Text(action!)),
        ]),
      );
}
