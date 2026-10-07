import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:songarr_app/api/models.dart';

void main() {
  test('the chosen name and icon survive a restart', () {
    final b = Brand(name: 'Harmony', iconId: 'abc123', icon: Uint8List.fromList([137, 80, 78, 71]), seeThrough: false);
    final again = Brand.fromJson(b.toJson());
    expect(again.name, 'Harmony');
    expect(again.iconId, 'abc123');
    expect(again.icon, [137, 80, 78, 71]);
    expect(again.seeThrough, isFalse);
  });

  test('Songarr until a server says otherwise', () {
    const b = Brand();
    expect(b.name, Brand.builtIn);
    expect(b.icon, isNull);
    expect(Brand.fromJson({}).name, Brand.builtIn);
  });
}
