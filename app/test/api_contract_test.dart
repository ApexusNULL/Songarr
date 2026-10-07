// Runs the app's API client against a real Songarr server.
// Skipped unless SONGARR_TEST_SERVER and SONGARR_TEST_CODE are set (a disposable test server
// and a fresh pairing code), e.g.:
//   SONGARR_TEST_SERVER=http://127.0.0.1:18486 SONGARR_TEST_CODE=ABCDEFGHJK flutter test test/api_contract_test.dart
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:songarr_app/api/client.dart';

void main() {
  final server = Platform.environment['SONGARR_TEST_SERVER'];
  final code = Platform.environment['SONGARR_TEST_CODE'];
  final skip = server == null || code == null ? 'no test server configured' : null;

  test('pair, browse, like, playlists, stream, request', () async {
    final pair = await SongarrApi.pair(server!, code!, 'contract test');
    final api = SongarrApi(server: server, token: pair.token);

    final me = await api.me();
    expect(me.userName, pair.userName);

    final home = await api.home();
    expect(home.sections, isA<List>());

    final lib = await api.library();
    final liked = await api.liked();
    expect(liked.total, lib.likedCount);
    expect(liked.tracks, isNotEmpty);

    final playable = liked.tracks.firstWhere((t) => t.playable);
    final client = HttpClient();
    final req = await client.getUrl(api.streamUri(playable.id));
    api.authHeaders.forEach(req.headers.set);
    req.headers.set('Range', 'bytes=0-99');
    final res = await req.close();
    expect(res.statusCode, 206);
    expect((await res.fold<List<int>>([], (a, b) => a..addAll(b))).length, 100);
    client.close();

    final pl = await api.createPlaylist('Contract test');
    final id = pl.info['id'] as String;
    await api.addToPlaylist(id, [playable.id]);
    expect((await api.playlist(id)).tracks.single.id, playable.id);
    await api.deletePlaylist(id);

    final unliked = liked.tracks.firstWhere((t) => !t.liked, orElse: () => playable);
    expect(await api.setLiked(unliked.id, true), isTrue);

    final search = await api.search(playable.title.split(' ').first);
    expect(search.tracks.map((t) => t.id), contains(playable.id));

    await api.reportPlay(playable.id, const Duration(seconds: 90), true);
    expect((await api.home()).sections.first.id, 'recently_played');

    final (source, results) = await api.catalogSearch('Fresh Tune');
    expect(source, 'deezer');
    final requested = await api.request(results.first);
    expect(requested.status, 'wanted');
    expect((await api.requests()).map((t) => t.id), contains(requested.id));

    await expectLater(SongarrApi(server: server, token: 'wrong').me(), throwsA(isA<ApiException>().having((e) => e.unauthorized, 'unauthorized', true)));
    await api.logout();
    await expectLater(api.me(), throwsA(isA<ApiException>()));
  }, skip: skip);
}
