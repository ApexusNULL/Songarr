import 'dart:math';

/// How much each song has been listened to lately: a decayed listen count.
///
/// Every listen adds its weight (1 for a full play, less for a partial one); the total halves
/// for every [halfLife] that passes. A song played weekly keeps a high score; one played once,
/// long ago, fades towards zero. The cache prunes the lowest scores first.
class ListenScores {
  ListenScores({this.halfLife = const Duration(days: 30)});

  final Duration halfLife;
  final Map<String, ({double score, DateTime at})> _entries = {};

  /// Score of [id] as of [now] (0 for songs never listened to).
  double score(String id, [DateTime? now]) {
    final e = _entries[id];
    if (e == null) return 0;
    final elapsed = (now ?? DateTime.now()).difference(e.at).inMilliseconds;
    if (elapsed <= 0) return e.score;
    return e.score * pow(0.5, elapsed / halfLife.inMilliseconds);
  }

  void add(String id, double weight, [DateTime? now]) {
    if (weight <= 0) return;
    final t = now ?? DateTime.now();
    _entries[id] = (score: score(id, t) + weight, at: t);
  }

  void remove(String id) => _entries.remove(id);

  /// Forget songs whose score has decayed to almost nothing (about 7 half-lives after one play).
  void compact([DateTime? now]) => _entries.removeWhere((id, _) => score(id, now) < 0.01);

  int get length => _entries.length;

  /// How much one listen counts: a play shorter than 30 s that also covers less than half
  /// the song (a skip) counts nothing; otherwise the fraction of the song heard.
  static double weightFor(Duration played, Duration length) {
    if (played < const Duration(seconds: 30) && (length == Duration.zero || played * 2 < length)) return 0;
    if (length == Duration.zero) return 1;
    return min(1.0, played.inMilliseconds / length.inMilliseconds);
  }

  /// [ids] ordered from first-to-prune to last: lowest score first; equal scores (e.g. songs
  /// never finished) fall back to [lastUsed], oldest first.
  List<String> pruneOrder(Iterable<String> ids, DateTime Function(String id) lastUsed, [DateTime? now]) {
    final t = now ?? DateTime.now();
    final list = ids.toList();
    final scores = {for (final id in list) id: score(id, t)};
    list.sort((a, b) {
      final c = scores[a]!.compareTo(scores[b]!);
      return c != 0 ? c : lastUsed(a).compareTo(lastUsed(b));
    });
    return list;
  }

  Map<String, dynamic> toJson() => {
        for (final e in _entries.entries) e.key: [e.value.score, e.value.at.millisecondsSinceEpoch],
      };

  void loadJson(Map<String, dynamic> json) {
    _entries.clear();
    json.forEach((id, v) {
      if (v is List && v.length == 2) {
        _entries[id] = (score: (v[0] as num).toDouble(), at: DateTime.fromMillisecondsSinceEpoch((v[1] as num).toInt()));
      }
    });
  }
}
