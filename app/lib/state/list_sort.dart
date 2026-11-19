import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/models.dart';

/// What a list of songs (a playlist, Liked Songs) can be sorted by. The list plays in the order shown.
enum SortBy {
  custom('Default order'),
  added('Date added'),
  title('Title'),
  artist('Artist'),
  album('Album'),
  released('Release date'),
  length('Length'),
  genre('Genre'),
  era('Era'),
  eraGenre('Era and genre'); // "80s Rap", "00s Pop"

  /// Sorts that put the list in groups (each with a heading, and a chip to show just that one).
  bool get groups => this == genre || this == era || this == eraGenre;

  const SortBy(this.label);
  final String label;
}

/// A list's sort: what by, and which way ([reversed]: the other way round from the usual, which is
/// newest first for dates, A to Z for names and shortest first for length).
class ListSort {
  const ListSort(this.by, {this.reversed = false});
  final SortBy by;
  final bool reversed;

  static const none = ListSort(SortBy.custom);

  String get direction => switch (by) {
        SortBy.custom => '',
        SortBy.added || SortBy.released || SortBy.era || SortBy.eraGenre => reversed ? 'oldest first' : 'newest first',
        SortBy.length => reversed ? 'longest first' : 'shortest first',
        _ => reversed ? 'Z to A' : 'A to Z',
      };

  Map<String, dynamic> toJson() => {'by': by.name, 'reversed': reversed};

  static ListSort fromJson(Object? j) {
    if (j is! Map) return none;
    final by = SortBy.values.where((b) => b.name == j['by']).firstOrNull;
    return by == null ? none : ListSort(by, reversed: j['reversed'] == true);
  }
}

/// The order to show [tracks] in under [sort], as positions in [tracks]. Songs without what's sorted
/// on (no release date, say) go last either way; ties keep the list's own order (an album's songs in
/// its order).
List<int> sortedOrder(List<Track> tracks, ListSort sort) {
  final order = [for (var i = 0; i < tracks.length; i++) i];
  if (sort.by == SortBy.custom) return order;
  final keys = [for (final t in tracks) _key(t, sort.by)];
  final newestFirst = const {SortBy.added, SortBy.released, SortBy.era, SortBy.eraGenre}.contains(sort.by);
  final descending = newestFirst != sort.reversed;
  order.sort((a, b) {
    final ka = keys[a], kb = keys[b];
    if (ka == null || kb == null) return ka == null && kb == null ? a - b : (ka == null ? 1 : -1);
    var c = ka.compareTo(kb);
    if (descending) c = -c;
    final ta = tracks[a], tb = tracks[b];
    if (c == 0 && sort.by == SortBy.eraGenre) c = _byGenre(ta, tb); // in each decade, genres A to Z
    if (c == 0 && (sort.by == SortBy.genre || sort.by == SortBy.eraGenre)) c = (tb.year ?? 0).compareTo(ta.year ?? 0); // then newest first
    if (c == 0 && sort.by != SortBy.added && sort.by != SortBy.title) c = _withinAlbum(ta, tb);
    return c != 0 ? c : a - b;
  });
  return order;
}

/// The heading of [t]'s group under a grouping sort ("80s Rap", "Pop", "90s"); null for other sorts.
String? groupOf(Track t, SortBy by) => switch (by) {
      SortBy.genre => t.genre ?? 'Genre not known yet',
      SortBy.era => t.era ?? 'Year not known yet',
      SortBy.eraGenre => t.era == null && t.genre == null ? 'Not known yet' : [?t.era, ?t.genre].join(' '),
      _ => null,
    };

/// The groups in [tracks] (already sorted), in the order they come, with how many songs each.
List<({String label, int count})> groupsIn(List<Track> tracks, SortBy by) {
  final counts = <String, int>{};
  for (final t in tracks) {
    final g = groupOf(t, by);
    if (g != null) counts[g] = (counts[g] ?? 0) + 1;
  }
  return [for (final e in counts.entries) (label: e.key, count: e.value)];
}

/// Genres A to Z, songs without one last.
int _byGenre(Track a, Track b) {
  final ga = a.genre, gb = b.genre;
  if (ga == null || gb == null) return ga == null ? (gb == null ? 0 : 1) : -1;
  return _words(ga).compareTo(_words(gb));
}

Comparable<Object>? _key(Track t, SortBy by) => switch (by) {
      SortBy.custom => null,
      SortBy.added => t.addedAt?.millisecondsSinceEpoch,
      SortBy.title => _words(t.title),
      SortBy.artist => t.artists.isEmpty ? null : _words(t.artists.first).replaceFirst(RegExp(r'^the '), ''),
      SortBy.album => t.album == null || t.album!.isEmpty ? null : _words(t.album!),
      SortBy.released => t.releaseDate == null || t.releaseDate!.isEmpty ? null : t.releaseDate, // "1984-02-27" or "1984"
      SortBy.length => t.duration == Duration.zero ? null : t.duration.inMilliseconds,
      SortBy.genre => t.genre == null ? null : _words(t.genre!),
      SortBy.era => t.year,
      SortBy.eraGenre => t.year == null ? (t.genre == null ? null : -1) : t.year! ~/ 10, // (a genre without a year: after the decades)
    };

String _words(String s) => s.trim().toLowerCase();

/// One artist's songs (or one day's, or one album's): by album, then in the album's own order.
int _withinAlbum(Track a, Track b) {
  final album = _words(a.album ?? '').compareTo(_words(b.album ?? ''));
  if (album != 0) return album;
  final disc = (a.discNumber ?? 0).compareTo(b.discNumber ?? 0);
  return disc != 0 ? disc : (a.trackNumber ?? 0).compareTo(b.trackNumber ?? 0);
}

/// The sort chosen for each list (`liked`, `playlist_{id}`), kept on this phone.
class ListSorts extends Notifier<Map<String, ListSort>> {
  static const _pref = 'list_sorts';

  @override
  Map<String, ListSort> build() {
    _load();
    return const {};
  }

  Future<void> _load() async {
    try {
      final raw = (await SharedPreferences.getInstance()).getString(_pref);
      if (raw == null) return;
      final kept = {for (final e in (jsonDecode(raw) as Map).entries) e.key as String: ListSort.fromJson(e.value)};
      state = {...kept, ...state}; // (one chosen meanwhile wins)
    } catch (_) {
      // storage unavailable or unreadable: lists show in their own order
    }
  }

  ListSort of(String key) => state[key] ?? ListSort.none;

  Future<void> choose(String key, ListSort sort) async {
    state = sort.by == SortBy.custom ? ({...state}..remove(key)) : {...state, key: sort};
    try {
      await (await SharedPreferences.getInstance())
          .setString(_pref, jsonEncode({for (final e in state.entries) e.key: e.value.toJson()}));
    } catch (_) {}
  }
}

final listSortsProvider = NotifierProvider<ListSorts, Map<String, ListSort>>(ListSorts.new);

/// Under a grouping sort, the one group of each list being shown ("80s Rap"), while the app runs.
class ListGroups extends Notifier<Map<String, String>> {
  @override
  Map<String, String> build() => const {};

  void show(String key, String? group) => state = group == null ? ({...state}..remove(key)) : {...state, key: group};
}

final listGroupsProvider = NotifierProvider<ListGroups, Map<String, String>>(ListGroups.new);
