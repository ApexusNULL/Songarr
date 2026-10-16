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
  length('Length');

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
        SortBy.added || SortBy.released => reversed ? 'oldest first' : 'newest first',
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
  final descending = (sort.by == SortBy.added || sort.by == SortBy.released) != sort.reversed;
  order.sort((a, b) {
    final ka = keys[a], kb = keys[b];
    if (ka == null || kb == null) return ka == null && kb == null ? a - b : (ka == null ? 1 : -1);
    var c = ka.compareTo(kb);
    if (descending) c = -c;
    if (c == 0 && sort.by != SortBy.added && sort.by != SortBy.title) c = _withinAlbum(tracks[a], tracks[b]);
    return c != 0 ? c : a - b;
  });
  return order;
}

Comparable<Object>? _key(Track t, SortBy by) => switch (by) {
      SortBy.custom => null,
      SortBy.added => t.addedAt?.millisecondsSinceEpoch,
      SortBy.title => _words(t.title),
      SortBy.artist => t.artists.isEmpty ? null : _words(t.artists.first).replaceFirst(RegExp(r'^the '), ''),
      SortBy.album => t.album == null || t.album!.isEmpty ? null : _words(t.album!),
      SortBy.released => t.releaseDate == null || t.releaseDate!.isEmpty ? null : t.releaseDate, // "1984-02-27" or "1984"
      SortBy.length => t.duration == Duration.zero ? null : t.duration.inMilliseconds,
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
