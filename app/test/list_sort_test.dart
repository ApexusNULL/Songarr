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

  group('by genre and era', () {
    Track tagged(String id, String? genre, int? year) =>
        Track({'id': id, 'title': id, 'artists': ['X'], 'genre': genre, 'year': year, 'release_date': '2011'});
    final mix = [
      tagged('rap88', 'Rap', 1988), tagged('pop03', 'Pop', 2003), tagged('rap85', 'Rap', 1985),
      tagged('rock75', 'Rock', 1975), tagged('pop81', 'Pop', 1981), tagged('none', null, null), tagged('pop?', 'Pop', null),
    ];
    List<String> sorted(ListSort s) => [for (final i in sortedOrder(mix, s)) mix[i].id];
    List<String> groups(ListSort s) => [for (final g in groupsIn([for (final i in sortedOrder(mix, s)) mix[i]], s.by)) '${g.label} ${g.count}'];

    test('decades as people say them', () {
      expect([for (final y in [1975, 1988, 2003, 2015, 2024, 1949]) tagged('x', null, y).era], ['70s', '80s', '00s', '10s', '20s', 'Older']);
    });

    test('era and genre together: "80s Rap", newest decade first, genres A to Z', () {
      const s = ListSort(SortBy.eraGenre);
      expect(sorted(s), ['pop03', 'pop81', 'rap88', 'rap85', 'rock75', 'pop?', 'none']);
      expect(groups(s), ['00s Pop 1', '80s Pop 1', '80s Rap 2', '70s Rock 1', 'Pop 1', 'Not known yet 1']);
      expect(sorted(const ListSort(SortBy.eraGenre, reversed: true)).take(4), ['pop?', 'rock75', 'pop81', 'rap88']);
    });

    test('by genre A to Z, newest first in each; by era', () {
      expect(groups(const ListSort(SortBy.genre)), ['Pop 3', 'Rap 2', 'Rock 1', 'Genre not known yet 1']);
      expect(sorted(const ListSort(SortBy.genre)).take(3), ['pop03', 'pop81', 'pop?']);
      expect(groups(const ListSort(SortBy.era)), ['00s 1', '80s 3', '70s 1', 'Year not known yet 2']);
      expect(groups(const ListSort(SortBy.era, reversed: true)), ['70s 1', '80s 3', '00s 1', 'Year not known yet 2']);
    });
  });
}
