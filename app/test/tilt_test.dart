import 'dart:math';

import 'package:flutter_test/flutter_test.dart';
import 'package:songarr_app/ui/fx/tilt.dart';

void main() {
  test('the depth effect keeps responding however far the phone turns', () {
    expect(Tilt.response(0), 0);
    expect(Tilt.response(-0.2), -Tilt.response(0.2));
    // small turns are nearly linear
    expect(Tilt.response(0.05) / 0.05, closeTo(Tilt.response(0.01) / 0.01, 0.1));
    // about the same strength as before for an ordinary 17° tilt
    expect(Tilt.response(17 * pi / 180), closeTo(0.69, 0.03));
    expect(Tilt.response(25 * pi / 180), closeTo(1.0, 0.06));
    // every further 5° still moves things noticeably (no hard stop): strongly up to 45°,
    // still visibly up to 80°
    var last = 0.0;
    for (var deg = 5; deg <= 80; deg += 5) {
      final r = Tilt.response(deg * pi / 180);
      expect(r - last, greaterThan(deg <= 45 ? 0.08 : 0.025), reason: 'flat at $deg°');
      last = r;
    }
    expect(last, lessThan(2));
  });
}
