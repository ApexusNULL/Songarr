import 'dart:async';
import 'dart:ui' as ui;

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

/// The most vivid colour in a piece of artwork, made bright enough to glow on a dark
/// background. Used to tint Now Playing and headers to match the cover.
final artColorProvider = FutureProvider.family<Color?, String>((ref, url) => artColor(url));

final _cache = <String, Color?>{};

Future<Color?> artColor(String url) async {
  if (_cache.containsKey(url)) return _cache[url];
  try {
    final provider = ResizeImage(CachedNetworkImageProvider(url), width: 24, height: 24);
    final done = Completer<ImageInfo>();
    final stream = provider.resolve(ImageConfiguration.empty);
    late final ImageStreamListener listener;
    listener = ImageStreamListener((info, _) {
      if (!done.isCompleted) done.complete(info);
      stream.removeListener(listener);
    }, onError: (e, s) {
      if (!done.isCompleted) done.completeError(e, s);
      stream.removeListener(listener);
    });
    stream.addListener(listener);
    final info = await done.future.timeout(const Duration(seconds: 10));
    final data = await info.image.toByteData(format: ui.ImageByteFormat.rawRgba);
    info.dispose();
    if (data == null) return _cache[url] = null;
    double r = 0, g = 0, b = 0, total = 0;
    for (var i = 0; i + 3 < data.lengthInBytes; i += 4) {
      final c = Color.fromARGB(255, data.getUint8(i), data.getUint8(i + 1), data.getUint8(i + 2));
      final hsv = HSVColor.fromColor(c);
      final w = hsv.saturation * hsv.saturation * hsv.value + 0.02; // vivid pixels count most
      r += c.r * w;
      g += c.g * w;
      b += c.b * w;
      total += w;
    }
    final avg = Color.from(alpha: 1, red: r / total, green: g / total, blue: b / total);
    final hsv = HSVColor.fromColor(avg);
    final out = hsv.withSaturation(hsv.saturation.clamp(0.45, 0.9)).withValue(hsv.value.clamp(0.6, 0.9)).toColor();
    return _cache[url] = out;
  } catch (_) {
    return _cache[url] = null;
  }
}
