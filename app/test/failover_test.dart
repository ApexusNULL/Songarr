import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:songarr_app/api/client.dart';

/// A tiny stand-in server: [standbyFor] makes it a backup standing by (it points at that server).
Future<HttpServer> fakeServer({String? standbyFor, List<String>? log}) async {
  final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
  server.listen((req) async {
    log?.add(req.uri.path);
    req.response.headers.contentType = ContentType.json;
    if (standbyFor != null) {
      req.response.statusCode = 503;
      req.response.write(jsonEncode({'error': 'standing by', 'standby': true, 'active': standbyFor}));
    } else if (req.headers.value('authorization') != 'Bearer t0ken') {
      req.response.statusCode = 401;
      req.response.write(jsonEncode({'error': 'not signed in'}));
    } else if (req.uri.path == '/api/v1/me') {
      req.response.write(jsonEncode({
        'user': {'id': 1, 'name': 'Alex', 'is_admin': true},
        'spotify_accounts': [],
        'server': {'name': 'Songarr', 'version': '0.1.0'},
      }));
    } else {
      req.response.write(jsonEncode({'answered_by': req.requestedUri.port}));
    }
    await req.response.close();
  });
  return server;
}

String address(HttpServer s) => 'http://127.0.0.1:${s.port}';

void main() {
  test('a standby points the app at the active server, which it then keeps using', () async {
    final active = await fakeServer();
    final standby = await fakeServer(standbyFor: address(active));
    final switched = <String>[];
    final api = SongarrApi(server: address(standby), token: 't0ken', alternatives: [address(active)], onSwitch: switched.add);
    final me = await api.me();
    expect(me.userName, 'Alex');
    expect(switched, [address(active)]);
    await active.close(force: true);
    await standby.close(force: true);
  });

  test('a server that has gone away: the next one that answers', () async {
    final gone = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final goneAddress = address(gone);
    await gone.close(force: true); // nothing listens there any more
    final backup = await fakeServer();
    final switched = <String>[];
    final api = SongarrApi(server: goneAddress, token: 't0ken', alternatives: [address(backup)], onSwitch: switched.add);
    expect((await api.me()).userId, 1);
    expect(switched, [address(backup)]);
    await backup.close(force: true);
  });

  test('without another server, the error is the error', () async {
    final standby = await fakeServer(standbyFor: 'https://elsewhere.example');
    final api = SongarrApi(server: address(standby), token: 't0ken');
    await expectLater(api.me(), throwsA(isA<ApiException>().having((e) => e.away, 'away', isTrue)));
    await standby.close(force: true);
  });
}
