import 'package:flutter_riverpod/flutter_riverpod.dart';
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

  group('genre cards, then eras', () {
    Track tagged(String id, String? genre, int? year) =>
        Track({'id': id, 'title': id, 'artists': ['X'], 'genre': genre, 'year': year, 'release_date': '2011'});
    final mix = [
      tagged('rap88', 'Rap', 1988), tagged('pop03', 'Pop', 2003), tagged('rap85', 'Rap', 1985), tagged('rock75', 'Rock', 1975),
      tagged('pop81', 'Pop', 1981), tagged('none', null, null), tagged('pop?', 'Pop', null), tagged('rap92', 'Rap', 1992),
      tagged('blues', 'Blues', 1999),
    ];
    String counted(List<({String label, int count})> l) => [for (final g in l) '${g.label} ${g.count}'].join(', ');
    List<String> shown(ListFilter? f) => [for (final i in filtered(mix, [for (var i = 0; i < mix.length; i++) i], f)) mix[i].id];

    test('decades as people say them', () {
      expect([for (final y in [1975, 1988, 2003, 2015, 2024, 1949]) tagged('x', null, y).era], ['70s', '80s', '00s', '10s', '20s', 'Older']);
    });

    test('a card for each genre, most songs first (then A to Z); songs not looked up yet in none', () {
      expect(counted(genresIn(mix)), 'Pop 3, Rap 3, Blues 1, Rock 1');
    });

    test("a genre's eras, oldest first", () {
      expect(counted(erasIn([for (final t in mix) if (t.genre == 'Rap') t])), '80s 2, 90s 1');
      expect(counted(erasIn([for (final t in mix) if (t.genre == 'Pop') t])), '80s 1, 00s 1'); // (one without a year: in neither)
    });

    test('a genre shows just its songs, an era just those of it, in the order shown', () {
      expect(shown(null).length, mix.length);
      expect(shown((genre: 'Rap', era: null)), ['rap88', 'rap85', 'rap92']);
      expect(shown((genre: 'Rap', era: '80s')), ['rap88', 'rap85']);
      expect(filtered(mix, [7, 2, 0], (genre: 'Rap', era: null)), [7, 2, 0]);
    });

    test('tap a genre, an era, then the genre again: the whole list', () {
      final c = ProviderContainer();
      addTearDown(c.dispose);
      final f = c.read(listFiltersProvider.notifier);
      f.genre('liked', 'Rap');
      f.era('liked', '80s');
      expect(c.read(listFiltersProvider)['liked'], (genre: 'Rap', era: '80s'));
      f.genre('liked', 'Pop'); // another genre: all its eras
      expect(c.read(listFiltersProvider)['liked'], (genre: 'Pop', era: null));
      f.genre('liked', 'Pop');
      expect(c.read(listFiltersProvider)['liked'], isNull);
      f.era('liked', '80s'); // (no genre: nothing to narrow)
      expect(c.read(listFiltersProvider)['liked'], isNull);
    });
  });
}
