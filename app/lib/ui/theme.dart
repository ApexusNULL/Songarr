import 'package:flutter/material.dart';

/// Songarr's look: a deep-space background with a slowly drifting aurora, frosted-glass
/// surfaces, rounded shapes and a violet → pink → amber "aurora" gradient for anything alive.

const accent = Color(0xFF8B5CF6); // violet, the brand colour
const accent2 = Color(0xFFEC4899); // pink
const accent3 = Color(0xFFF59E0B); // amber
const glowColor = Color(0xFFC4B5FD); // light violet, for sparkles and glows
const background = Color(0xFF07060F);
const surface = Color(0xFF14122A);
const surfaceHigh = Color(0xFF221E3F);
const glassFill = Color(0x14FFFFFF);
const glassBorder = Color(0x22FFFFFF);
const muted = Color(0xFFA9A3C7);

const aurora = LinearGradient(colors: [accent, accent2, accent3]);
const auroraSoft = LinearGradient(colors: [accent, accent2]);

const sparkleColors = [Colors.white, glowColor, Color(0xFFF9A8D4), Color(0xFFFCD34D)];

// Each genre gets its own two-colour glow.
const genreGradients = [
  [Color(0xFF8B5CF6), Color(0xFFEC4899)],
  [Color(0xFFF97316), Color(0xFFDB2777)],
  [Color(0xFF2563EB), Color(0xFF7C3AED)],
  [Color(0xFF059669), Color(0xFF0EA5E9)],
  [Color(0xFFE11D48), Color(0xFFF59E0B)],
  [Color(0xFF16A34A), Color(0xFF84CC16)],
  [Color(0xFFB45309), Color(0xFFEAB308)],
  [Color(0xFF6D28D9), Color(0xFF312E81)],
  [Color(0xFF0284C7), Color(0xFF22D3EE)],
  [Color(0xFFA21CAF), Color(0xFF6366F1)],
  [Color(0xFFDC2626), Color(0xFF7F1D1D)],
  [Color(0xFF0F766E), Color(0xFF4F46E5)],
];

/// A genre's colours by its name, the same in every list: the usual ones picked to suit them, any
/// other by its letters.
List<Color> genreColors(String name) {
  const picked = {
    'Pop': 0, 'Rap': 1, 'Dance': 8, 'Electronic': 3, 'Rock': 10, 'Alternative': 5, 'R&B': 9,
    'Soul & Funk': 6, 'Jazz': 6, 'Classical': 7, 'Country': 6, 'Metal': 10, 'Reggae': 5,
    'Latin': 4, 'Soundtracks': 2, 'Blues': 2, 'Folk': 11, 'Kids': 4,
  };
  final i = picked[name] ?? name.codeUnits.fold<int>(7, (h, c) => (h * 31 + c) & 0x7fffffff);
  return genreGradients[i % genreGradients.length];
}

const radiusL = 24.0;
const radiusM = 16.0;
const radiusS = 12.0;

ThemeData songarrTheme() {
  final scheme = ColorScheme.fromSeed(seedColor: accent, brightness: Brightness.dark).copyWith(
    primary: accent,
    secondary: accent2,
    tertiary: accent3,
    surface: background,
    surfaceContainerLowest: background,
    surfaceContainerLow: surface,
    surfaceContainer: surface,
    surfaceContainerHigh: surfaceHigh,
    surfaceContainerHighest: surfaceHigh,
    onSurfaceVariant: muted,
  );
  final base = ThemeData(useMaterial3: true, colorScheme: scheme, brightness: Brightness.dark);
  return base.copyWith(
    scaffoldBackgroundColor: Colors.transparent, // the nebula shows through every page
    canvasColor: surface,
    textTheme: base.textTheme.copyWith(
      headlineLarge: const TextStyle(fontSize: 34, fontWeight: FontWeight.w800, letterSpacing: -1.0, height: 1.05),
      headlineMedium: const TextStyle(fontSize: 26, fontWeight: FontWeight.w800, letterSpacing: -0.6),
      titleLarge: const TextStyle(fontSize: 21, fontWeight: FontWeight.w800, letterSpacing: -0.4),
      titleMedium: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600, letterSpacing: -0.1),
    ),
    appBarTheme: const AppBarTheme(
      backgroundColor: Colors.transparent,
      surfaceTintColor: Colors.transparent,
      scrolledUnderElevation: 0,
      centerTitle: false,
      titleTextStyle: TextStyle(fontSize: 20, fontWeight: FontWeight.w800, letterSpacing: -0.4, color: Colors.white),
    ),
    listTileTheme: const ListTileThemeData(
      iconColor: muted,
      subtitleTextStyle: TextStyle(color: muted, fontSize: 13),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(radiusM))),
    ),
    bottomSheetTheme: const BottomSheetThemeData(
      backgroundColor: surface,
      surfaceTintColor: Colors.transparent,
      showDragHandle: true,
      dragHandleColor: Color(0x55FFFFFF),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(28))),
    ),
    dialogTheme: const DialogThemeData(
      backgroundColor: surface,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(radiusL))),
    ),
    snackBarTheme: const SnackBarThemeData(
      behavior: SnackBarBehavior.floating,
      backgroundColor: surfaceHigh,
      contentTextStyle: TextStyle(color: Colors.white),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(radiusM))),
    ),
    chipTheme: base.chipTheme.copyWith(
      backgroundColor: glassFill,
      side: const BorderSide(color: glassBorder),
      shape: const StadiumBorder(),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(shape: const StadiumBorder(), padding: const EdgeInsets.symmetric(horizontal: 22, vertical: 14)),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        shape: const StadiumBorder(),
        side: const BorderSide(color: glassBorder),
        foregroundColor: Colors.white,
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: glassFill,
      border: OutlineInputBorder(borderRadius: BorderRadius.circular(radiusM), borderSide: const BorderSide(color: glassBorder)),
      enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(radiusM), borderSide: const BorderSide(color: glassBorder)),
      focusedBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(radiusM), borderSide: const BorderSide(color: accent, width: 1.5)),
    ),
    progressIndicatorTheme: const ProgressIndicatorThemeData(color: accent),
    pageTransitionsTheme: const PageTransitionsTheme(builders: {
      TargetPlatform.android: DriftTransitionsBuilder(),
      TargetPlatform.iOS: DriftTransitionsBuilder(),
      TargetPlatform.windows: DriftTransitionsBuilder(),
      TargetPlatform.linux: DriftTransitionsBuilder(),
      TargetPlatform.macOS: DriftTransitionsBuilder(),
    }),
  );
}

/// Pages drift in: the new page fades up from slightly smaller while the old one fades back,
/// over a background that never moves.
class DriftTransitionsBuilder extends PageTransitionsBuilder {
  const DriftTransitionsBuilder();

  @override
  Duration get transitionDuration => const Duration(milliseconds: 380);

  @override
  Widget buildTransitions<T>(PageRoute<T> route, BuildContext context, Animation<double> animation,
      Animation<double> secondaryAnimation, Widget child) {
    final inCurve = CurvedAnimation(parent: animation, curve: Curves.easeOutCubic, reverseCurve: Curves.easeInCubic);
    final outCurve = CurvedAnimation(parent: secondaryAnimation, curve: Curves.easeOutCubic);
    return FadeTransition(
      opacity: Tween(begin: 1.0, end: 0.0).animate(outCurve),
      child: ScaleTransition(
        scale: Tween(begin: 1.0, end: 1.03).animate(outCurve),
        child: FadeTransition(
          opacity: inCurve,
          child: SlideTransition(
            position: Tween(begin: const Offset(0, 0.04), end: Offset.zero).animate(inCurve),
            child: ScaleTransition(scale: Tween(begin: 0.96, end: 1.0).animate(inCurve), child: child),
          ),
        ),
      ),
    );
  }
}
