import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:songarr_app/api/models.dart';
import 'package:songarr_app/ui/download_progress.dart';
import 'package:songarr_app/ui/theme.dart';

DownloadProgress _progress(Map<String, dynamic> over) => DownloadProgress({
      'total': 10, 'ready': 2, 'waiting': 6, 'later': 0, 'stuck': 0, 'paused': null, 'eta': 600,
      'active': [
        {'id': 'a', 'title': 'Hit One', 'artists': ['Nova Band'], 'stage': 'downloading', 'percent': 50.0},
        {'id': 'b', 'title': 'Deep Cut', 'artists': ['Nova Band'], 'stage': 'finding', 'percent': 0.0},
      ],
      ...over,
    });

void main() {
  test('what the songs you added are doing, in words', () {
    final p = _progress({});
    expect(p.headline, '2 of 10 songs ready');
    expect(p.detail, 'About 10 min left');
    expect(p.fraction, closeTo((2 + 0.45) / 10, 1e-9)); // (a song half downloaded is a little under half done)
    expect(p.of('a')!.stageText, 'Downloading · 50%');
    expect(p.of('b')!.stageText, 'Finding it on YouTube');
    expect(p.of('c'), isNull);

    expect(_progress({'ready': 0}).headline, 'Downloading 10 songs');
    expect(_progress({'active': []}).headline, '8 songs waiting to download');
    expect(_progress({'eta': 90}).detail, ''); // (not worth saying)
    expect(_progress({'eta': 3 * 3600 + 1800, 'later': 1, 'stuck': 2}).detail,
        'About 3.5 h left · 1 to try again later · 2 need a hand on the server');
    final one = _progress({'total': 1, 'ready': 0, 'active': [
      {'id': 'a', 'title': 'Hit One', 'stage': 'finishing', 'percent': 100.0},
    ]});
    expect(one.headline, 'Hit One is almost ready');

    final until = DateTime(2026, 11, 7, 15, 40);
    final paused = _progress({'active': [], 'paused': {'reason': 'YouTube asked Songarr to slow down', 'until': until.millisecondsSinceEpoch / 1000}});
    expect(paused.headline, 'Waiting to download 8 songs');
    expect(paused.detail, 'YouTube asked Songarr to slow down; carrying on at 15:40');
    expect(_progress({'paused': {'reason': 'Downloads are paused on the server', 'until': null}, 'eta': null}).detail,
        'Downloads are paused on the server');
  });

  testWidgets('the card at the top of Requested songs', (tester) async {
    await tester.pumpWidget(MaterialApp(
      theme: songarrTheme(),
      home: Scaffold(body: DownloadProgressCard(_progress({'stuck': 1}))),
    ));
    await tester.pumpAndSettle();
    expect(find.text('2 of 10 songs ready'), findsOneWidget);
    expect(find.text('24%'), findsOneWidget);
    expect(find.text('About 10 min left · 1 needs a hand on the server'), findsOneWidget);
    final bar = tester.widget<FractionallySizedBox>(find.descendant(of: find.byType(DownloadBar), matching: find.byType(FractionallySizedBox)));
    expect(bar.widthFactor, closeTo(0.245, 1e-9));
  });
}
