import 'package:flutter_test/flutter_test.dart';
import 'package:songarr_app/state/listen_scores.dart';

void main() {
  final t0 = DateTime(2026, 1, 1);
  DateTime day(int n) => t0.add(Duration(days: n));

  group('weightFor', () {
    test('skips count nothing, full plays count 1, partial plays count their fraction', () {
      const song = Duration(minutes: 4);
      expect(ListenScores.weightFor(const Duration(seconds: 10), song), 0);
      expect(ListenScores.weightFor(const Duration(seconds: 29), song), 0);
      expect(ListenScores.weightFor(song, song), 1);
      expect(ListenScores.weightFor(const Duration(minutes: 2), song), closeTo(0.5, 1e-9));
      expect(ListenScores.weightFor(const Duration(minutes: 9), song), 1); // repeat-one etc. never exceeds 1
    });

    test('short songs count once more than half is heard', () {
      const short = Duration(seconds: 40);
      expect(ListenScores.weightFor(const Duration(seconds: 15), short), 0);
      expect(ListenScores.weightFor(const Duration(seconds: 25), short), closeTo(25 / 40, 1e-9));
    });
  });

  test('scores halve every half-life', () {
    final s = ListenScores(halfLife: const Duration(days: 30));
    s.add('a', 1, t0);
    expect(s.score('a', t0), 1);
    expect(s.score('a', day(30)), closeTo(0.5, 1e-9));
    expect(s.score('a', day(60)), closeTo(0.25, 1e-9));
    s.add('a', 1, day(30));
    expect(s.score('a', day(30)), closeTo(1.5, 1e-9));
  });

  test('a weekly favourite outlasts a song played once yesterday', () {
    final s = ListenScores();
    for (var w = 0; w < 10; w++) {
      s.add('favourite', 1, day(w * 7)); // played every week for ten weeks
    }
    s.add('one-off', 1, day(69)); // played once, the day before "now"
    final now = day(70);
    expect(s.score('favourite', now), greaterThan(s.score('one-off', now)));
    final order = s.pruneOrder(['favourite', 'one-off', 'never-played'], (_) => t0, now);
    expect(order, ['never-played', 'one-off', 'favourite']);
  });

  test('equal scores fall back to least recently used', () {
    final s = ListenScores();
    final used = {'old': day(1), 'new': day(5)};
    expect(s.pruneOrder(['new', 'old'], (id) => used[id]!, day(6)), ['old', 'new']);
  });

  test('round-trips through JSON and forgets fully faded songs', () {
    final s = ListenScores()
      ..add('kept', 2, day(100))
      ..add('faded', 1, t0);
    final copy = ListenScores()..loadJson(s.toJson());
    expect(copy.score('kept', day(100)), 2);
    copy.compact(day(100)); // 'faded' is down to 1/2^(100/30) ≈ 0.1: still kept
    expect(copy.length, 2);
    copy.compact(day(300)); // 'faded' ≈ 0.001: forgotten; 'kept' ≈ 2/2^(200/30) ≈ 0.02: still kept
    expect(copy.score('faded', day(300)), 0);
    expect(copy.length, 1);
  });
}
