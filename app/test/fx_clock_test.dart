import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:songarr_app/ui/fx/clock.dart';

class _Effect extends StatefulWidget {
  const _Effect({this.wants = true});
  final bool wants;

  @override
  State<_Effect> createState() => _EffectState();
}

class _EffectState extends State<_Effect> with FxUser {
  @override
  bool get wantsClock => widget.wants;

  @override
  void didUpdateWidget(_Effect old) {
    super.didUpdateWidget(old);
    syncClock();
  }

  @override
  Widget build(BuildContext context) => const SizedBox();
}

Widget _app({bool shown = true, bool wants = true, bool still = false}) => MediaQuery(
      data: MediaQueryData(disableAnimations: still),
      child: TickerMode(enabled: shown, child: _Effect(wants: wants)),
    );

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final clock = FxClock.instance;

  testWidgets('the effects clock runs only while an effect on screen wants it', (tester) async {
    await tester.pumpWidget(_app());
    expect(clock.running.value, isTrue);
    await tester.pumpWidget(_app(shown: false)); // a hidden tab, or a page underneath another
    expect(clock.running.value, isFalse);
    await tester.pumpWidget(_app(wants: false)); // the turning disc, say, with the music paused
    expect(clock.running.value, isFalse);
    await tester.pumpWidget(_app(still: true)); // the phone asks for reduced motion
    expect(clock.running.value, isFalse);
    await tester.pumpWidget(_app());
    expect(clock.running.value, isTrue);
    await tester.pumpWidget(const SizedBox()); // gone
    expect(clock.running.value, isFalse);
  });

  testWidgets('it ticks a few times a second, not every frame', (tester) async {
    var ticks = 0;
    void count() => ticks++;
    clock.addListener(count);
    await tester.pumpWidget(_app());
    final before = clock.seconds;
    await tester.pump(const Duration(seconds: 1));
    expect(ticks, FxClock.fps);
    expect(clock.seconds, greaterThan(before));
    clock.removeListener(count);
    await tester.pumpWidget(const SizedBox());
  });
}
