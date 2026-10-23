import 'dart:io';

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// The desktop app's window (Windows): its title follows the server's name for the app.
class DesktopWindow {
  static const _ch = MethodChannel('songarr/window');
  static String? _title;

  static bool get isDesktop => Platform.isWindows || Platform.isLinux || Platform.isMacOS;

  /// What to call the device the app is on, in the app's words ("On this phone").
  static String get device => isDesktop ? 'computer' : 'phone';

  static void setTitle(String title) {
    if (!Platform.isWindows || title == _title) return;
    _title = title;
    _ch.invokeMethod<void>('setTitle', title).catchError((_) {});
  }
}

/// On a computer, lists also scroll by dragging with the mouse, as with a finger: so pulling down
/// refreshes, and the sideways shelves move (a mouse wheel only scrolls up and down).
class MouseDragScrollBehavior extends MaterialScrollBehavior {
  const MouseDragScrollBehavior();

  @override
  Set<PointerDeviceKind> get dragDevices => {...super.dragDevices, PointerDeviceKind.mouse};
}
