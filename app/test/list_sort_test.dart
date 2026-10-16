import 'package:flutter_test/flutter_test.dart';
import 'package:songarr_app/api/models.dart';
import 'package:songarr_app/state/list_sort.dart';

Track song(String id, String title, String artist,
        {String? album, String? released, int? track, int seconds = 200, num? added}) =>
    Track({
      'id': id, 'title': title, 'artists': [artist], 'album': album, 'release_date': released,
      'track_number': track, 'duration_ms': seconds * 1000, 'added_at': added,
    });

void main() {
  // in the playlist's own order
  final list = [
    song('a', 'Radio Ga Ga', 'Queen', album: 'The Works', released: '1984-02-27', track: 1, seconds: 348, added: 300),
    song('b', 'bohemian Rhapsody', 'Queen', album: 'A Night at the Opera', released: '1975', track: 11, seconds: 354, added: 100),
    song('c', 'Hey Jude', 'The Beatles', album: 'Hey Jude', released: '1968-08-26', track: 1, seconds: 431, added: 200),
    song('d', 'Something', 'The Beatles', album: 'Abbey Road', released: '1969-09-26', track: 2, seconds: 182),
    song('e', 'Hammer to Fall', 'Queen', album: 'The Works', released: '1984-02-27', track: 9, seconds: 220, added: 250),
  ];
  List<String> ids(ListSort s) => [for (final i in sortedOrder(list, s)) list[i].id];

  test('the default is the list\'s own order', () {
    expect(ids(ListSort.none), ['a', 'b', 'c', 'd', 'e']);
  });

  test('by date added, newest first; songs without a date last either way', () {
    expect(ids(const ListSort(SortBy.added)), ['a', 'e', 'c', 'b', 'd']);
    expect(ids(const ListSort(SortBy.added, reversed: true)), ['b', 'c', 'e', 'a', 'd']);
  });

  test('by title, A to Z whatever the capitals', () {
    expect(ids(const ListSort(SortBy.title)), ['b', 'e', 'c', 'a', 'd']);
    expect(ids(const ListSort(SortBy.title, reversed: true)), ['d', 'a', 'c', 'e', 'b']);
  });

  test('by artist ("The Beatles" under B), each one\'s albums in their own order', () {
    expect(ids(const ListSort(SortBy.artist)), ['d', 'c', 'b', 'a', 'e']);
  });

  test('by album, its songs in the album\'s order', () {
    expect(ids(const ListSort(SortBy.album)), ['b', 'd', 'c', 'a', 'e']);
  });

  test('by release date, newest first (a year alone counts too)', () {
    expect(ids(const ListSort(SortBy.released)), ['a', 'e', 'b', 'd', 'c']);
    expect(ids(const ListSort(SortBy.released, reversed: true)), ['c', 'd', 'b', 'a', 'e']);
  });

  test('by length, shortest first', () {
    expect(ids(const ListSort(SortBy.length)), ['d', 'e', 'a', 'b', 'c']);
  });

  test('a sort is kept as it was chosen', () {
    const s = ListSort(SortBy.released, reversed: true);
    final back = ListSort.fromJson(s.toJson());
    expect((back.by, back.reversed), (SortBy.released, true));
    expect(ListSort.fromJson({'by': 'something new'}).by, SortBy.custom);
  });
}
