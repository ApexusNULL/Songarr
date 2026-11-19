import 'package:flutter/material.dart';

import '../api/models.dart';
import 'fx/motion.dart';
import 'theme.dart';

/// A slim aurora bar, filled to [value] (0..1). Moves smoothly to a new value, then rests.
class DownloadBar extends StatelessWidget {
  const DownloadBar({super.key, required this.value, this.height = 4});
  final double value;
  final double height;

  @override
  Widget build(BuildContext context) => ClipRRect(
        borderRadius: BorderRadius.circular(height),
        child: SizedBox(
          height: height,
          child: Stack(fit: StackFit.expand, children: [
            const ColoredBox(color: glassFill),
            TweenAnimationBuilder<double>(
              tween: Tween(end: value.clamp(0, 1)),
              duration: const Duration(milliseconds: 500),
              curve: Curves.easeOutCubic,
              builder: (_, v, _) => FractionallySizedBox(
                alignment: Alignment.centerLeft,
                widthFactor: v,
                child: const DecoratedBox(decoration: BoxDecoration(gradient: aurora)),
              ),
            ),
          ]),
        ),
      );
}

/// The songs you added, on their way: "12 of 547 songs ready", the bar, and the time left or why
/// it's waiting. At the top of Requested songs.
class DownloadProgressCard extends StatelessWidget {
  const DownloadProgressCard(this.progress, {super.key});
  final DownloadProgress progress;

  @override
  Widget build(BuildContext context) {
    final p = progress;
    final detail = p.detail;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 4, 16, 10),
      child: Glass(
        blur: false,
        radius: radiusM,
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            GradientMask(child: Icon(p.pausedReason != null ? Icons.pause_circle_rounded : Icons.downloading_rounded, size: 22)),
            const SizedBox(width: 10),
            Expanded(
              child: Text(p.headline, maxLines: 2, overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 15.5)),
            ),
            if (p.total > 1) Text('${(p.fraction * 100).floor()}%', style: const TextStyle(color: muted, fontWeight: FontWeight.w700)),
          ]),
          const SizedBox(height: 12),
          DownloadBar(value: p.fraction, height: 6),
          if (detail.isNotEmpty) ...[
            const SizedBox(height: 10),
            Text(detail, style: const TextStyle(color: muted, fontSize: 13, height: 1.3)),
          ],
        ]),
      ),
    );
  }
}
