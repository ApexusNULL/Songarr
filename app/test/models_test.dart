import 'package:flutter_test/flutter_test.dart';
import 'package:songarr_app/api/client.dart';
import 'package:songarr_app/api/models.dart';

void main() {
  group('pairing QR codes', () {
    test('parse the server address and code', () {
      final p = PairingInfo.fromQr('songarr://pair?server=https%3A%2F%2Fmusic.example.com%2F&code=ABCDEFGHJK')!;
      expect(p.server, 'https://music.example.com');
      expect(p.code, 'ABCDEFGHJK');
    });

    test('reject other QR codes', () {
      expect(PairingInfo.fromQr('https://example.com'), isNull);
      expect(PairingInfo.fromQr('songarr://pair?server=x'), isNull);
      expect(PairingInfo.fromQr('not a url at all'), isNull);
    });

    test('server addresses are normalised', () {
      expect(PairingInfo.normalizeServer(' music.example.com/ '), 'https://music.example.com');
      expect(PairingInfo.normalizeServer('http://10.0.2.2:8486'), 'http://10.0.2.2:8486');
      expect(PairingInfo.normalizeServer(''), '');
    });
  });

  test('tracks read the server JSON (docs/API.md)', () {
    final t = Track.fromJson({
      'id': '4uLU6hMCjMI75M1A2tKUQC', 'title': 'Song', 'artists': ['A', 'B'], 'album': 'Al', 'album_id': 'x',
      'album_artists': ['A'], 'duration_ms': 125000, 'explicit': true, 'cover_url': 'https://c/640', 'thumb_url': null,
      'status': 'downloading', 'playable': false, 'format': 'm4a', 'size': null, 'liked': true,
    });
    expect(t.artistLine, 'A, B');
    expect(t.duration, const Duration(seconds: 125));
    expect(t.thumbUrl, 'https://c/640'); // falls back to the full cover
    expect(t.playable, isFalse);
    expect(t.statusText, contains('Downloading'));
    expect(t.copyWith(liked: false).liked, isFalse);
    expect(t.copyWith().liked, isTrue);
  });

  test('home reads made-for-you shelves and podcasts', () {
    final h = Home.fromJson({
      'sections': [],
      'playlists': [],
      'liked_count': 12,
      'for_you': {
        'songs': [
          {
            'source': 'deezer', 'id': 11, 'title': 'Paper Skies', 'artists': ['The Lanterns'], 'cover_url': 'https://c/1',
            'reason': 'Because you like Northbound', 'in_library': null, 'track': null,
          },
          {
            'source': 'library', 'id': 'abc', 'title': 'Satellites', 'artists': ['Northbound'], 'in_library': 'abc',
            'track': {'id': 'abc', 'title': 'Satellites', 'artists': ['Northbound'], 'playable': true, 'status': 'downloaded'},
          },
        ],
        'artists': [
          {'name': 'The Lanterns', 'image_url': 'https://i/1', 'reason': 'Because you like Northbound', 'songs_on_server': 2},
        ],
        'top_artists': [],
        'podcasts': [
          {'id': 'ap1', 'title': 'The Daily', 'following': false, 'reason': 'Popular right now'},
        ],
      },
      'podcasts': {
        'following': [],
        'continue': [
          {'id': 'ep1', 'podcast_id': 'ap9', 'podcast_title': 'History Hour', 'title': 'Rome', 'duration_ms': 600000,
           'progress_ms': 150000, 'completed': false, 'published': 1790000000},
        ],
        'new_episodes': [],
      },
    });
    expect(h.likedCount, 12);
    final songs = h.forYou!.songs;
    expect(songs[0].playable, isFalse);
    expect(songs[0].catalogItem.source, 'deezer');
    expect(songs[1].playable, isTrue);
    expect(songs[1].track!.id, 'abc');
    expect(h.forYou!.artists.single.songsOnServer, 2);
    expect(h.forYou!.podcasts.single.reason, 'Popular right now');
    final e = h.podcasts.continueListening.single;
    expect(e.fraction, closeTo(0.25, 0.001));
    final t = e.toTrack();
    expect(t.isEpisode, isTrue);
    expect(t.progress, const Duration(minutes: 2, seconds: 30));
    expect(t.artistLine, 'History Hour');
    expect(t.playable, isTrue);
  });

  test('home without recommendations yet', () {
    final h = Home.fromJson({'sections': [], 'playlists': [], 'for_you': null});
    expect(h.forYou, isNull);
    expect(h.podcasts.following, isEmpty);
  });

  test('previews and artist pages', () {
    final item = CatalogItem({'source': 'deezer', 'id': 11, 'title': 'Paper Skies', 'artists': ['The Lanterns'], 'cover_url': 'https://c/1'});
    final p = item.previewTrack(Uri.parse('https://s/api/v1/preview?source=deezer&id=11'));
    expect(p.isPreview, isTrue);
    expect(p.id, 'preview-deezer-11');
    expect(p.coverUrl, 'https://c/1');
    final a = ArtistPage.fromJson({
      'name': 'Northbound',
      'tracks': [],
      'albums': [],
      'about': {'name': 'Northbound', 'image_url': 'https://i/404', 'fans': 5000000},
      'popular': [
        {'source': 'deezer', 'id': 16, 'title': 'Satellites', 'artists': ['Northbound'], 'in_library': 'abc', 'status': 'downloaded'},
      ],
      'related': [
        {'name': 'Glasswing'},
      ],
    });
    expect(a.about!.fans, 5000000);
    expect(a.popular.single.inLibrary, 'abc');
    expect(a.related.single.name, 'Glasswing');
  });

  test('artist stories and lyrics', () {
    final band = ArtistAbout({
      'description': 'American rock band',
      'summary': 'A band.',
      'history': [
        {'heading': '2006–2009: Early years', 'text': 'They formed.'},
        {'heading': '', 'text': 'Then more happened.'},
      ],
      'facts': {'type': 'group', 'formed': '2006', 'origin': 'Portland, United States', 'genres': ['emo'], 'members': ['Robin']},
    });
    expect(band.origin, 'Formed 2006 · Portland, United States');
    expect(band.history.first.heading, '2006–2009: Early years');
    expect(band.members, ['Robin']);
    final singer = ArtistAbout({'summary': 'A rapper.', 'facts': {'type': 'person', 'born': '1972', 'died': '2020'}});
    expect(singer.origin, 'Born 1972 (died 2020)');
    expect(ArtistAbout({'summary': 'x'}).origin, isNull);

    final l = Lyrics({
      'synced': [
        {'t': 1500, 'text': 'One'},
        {'t': 30000, 'text': 'Two'},
      ],
      'plain': 'One\nTwo',
      'instrumental': false,
      'source': 'LRCLIB',
    });
    expect(l.isSynced, isTrue);
    expect(l.synced.map((x) => x.at.inMilliseconds), [1500, 30000]);
    expect(Lyrics({'synced': null, 'plain': 'words'}).isSynced, isFalse);
  });

  test('a Jam: the shared timeline', () {
    final song = {'id': 's1', 'title': 'One', 'artists': ['A'], 'duration_ms': 60000, 'status': 'downloaded', 'playable': true};
    final jam = Jam({
      'id': 'abcdef012345',
      'host': {'id': 2, 'name': 'Sam'},
      'members': [
        {'id': 2, 'name': 'Sam'},
        {'id': 1, 'name': 'Alex'},
      ],
      'invited': [],
      'queue': [song],
      'index': 0,
      'playing': true,
      'position_ms': 5000,
      'ref': 1000000,
      'server_time': 999000,
      'version': 3,
    });
    expect(jam.host.name, 'Sam');
    expect(jam.members.map((m) => m.name), ['Sam', 'Alex']);
    expect(jam.current?.title, 'One');
    expect(jam.positionAt(999500), const Duration(seconds: 5)); // not started yet: waits at the start point
    expect(jam.positionAt(1002500), const Duration(milliseconds: 7500));
    final invite = JamInvite({'id': 'abcdef012345', 'from': 'Sam', 'item': song});
    expect((invite.from, invite.item?.title), ('Sam', 'One'));
  });

  test('a catalogue album and its track list', () {
    final a = CatalogAlbumPage({
      'source': 'deezer', 'id': 7001, 'name': 'Space Score (Original Motion Picture Soundtrack)', 'artists': ['Ada Composer'],
      'type': 'album', 'year': '2014', 'count': 3, 'on_server': 1, 'added': false,
      'tracks': [
        {'source': 'deezer', 'id': 1, 'title': 'Main Title', 'artists': ['Ada Composer'], 'duration_ms': 120000, 'track_number': 1, 'disc_number': 1},
        {'source': 'deezer', 'id': 2, 'title': 'Cue 2', 'artists': ['Ada Composer'], 'duration_ms': 3540000, 'track_number': 2, 'disc_number': 1,
         'in_library': 's2', 'status': 'downloaded', 'track': {'id': 's2', 'title': 'Cue 2', 'artists': ['Ada Composer'], 'status': 'downloaded', 'playable': true}},
        {'source': 'deezer', 'id': 3, 'title': 'Finale', 'artists': ['Ada Composer'], 'duration_ms': 60000, 'track_number': 1, 'disc_number': 2},
      ],
    });
    expect((a.id, a.typeLabel, a.discs, a.count, a.added), ('7001', 'Album', 2, 3, false));
    expect(a.subtitle, 'Album · 2014 · 3 songs · 1 h 2 min · 1 on your server');
    expect(a.tracks[1].track?.playable, isTrue);
    expect(a.tracks[0].track, isNull);
    expect((a.tracks[2].discNumber, a.tracks[2].trackNumber), (2, 1));
    expect(CatalogAlbum({'id': 9, 'name': 'X', 'type': 'compile'}).typeLabel, 'Compilation');
  });

  test('the versions of a song, to replace a wrong download', () {
    final v = SongVersions({
      'current': 'PvizEDA1Nkw',
      'current_url': 'https://youtu.be/PvizEDA1Nkw',
      'expected_s': 348.173,
      'versions': [
        {'youtube_id': 'PvizEDA1Nkw', 'title': 'Radio Ga Ga', 'channel': 'Queen at The Opera Original Cast - Topic', 'duration_s': 352,
         'score': 0.6, 'official': true, 'current': true, 'url': 'https://youtu.be/PvizEDA1Nkw'},
        {'youtube_id': 'g9L-hOwKcHQ', 'title': 'Radio Ga Ga', 'channel': 'Queen', 'duration_s': 349, 'score': 1.0, 'official': false,
         'current': false, 'url': 'https://youtu.be/g9L-hOwKcHQ'},
      ],
    });
    expect((v.current, v.expected?.inSeconds, v.versions.length), ('PvizEDA1Nkw', 348, 2));
    final cast = v.versions.first, queen = v.versions.last;
    expect((cast.channel, cast.current, cast.official), ('Queen at The Opera Original Cast', true, true)); // ' - Topic' dropped
    expect((queen.youtubeId, queen.duration?.inSeconds, queen.current, queen.url), ('g9L-hOwKcHQ', 349, false, 'https://youtu.be/g9L-hOwKcHQ'));
  });
}
