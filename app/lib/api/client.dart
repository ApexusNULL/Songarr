import 'dart:typed_data';

import 'package:dio/dio.dart';

import 'models.dart';

class ApiException implements Exception {
  ApiException(this.message, {this.status, this.away = false, this.active});
  final String message;
  final int? status;

  /// The server couldn't be reached, or is a backup standing by: another server may answer.
  final bool away;

  /// A standby's pointer to the server that's active now.
  final String? active;

  bool get unauthorized => status == 401;

  @override
  String toString() => message;
}

/// What a pairing QR code (or a typed code) tells the app.
class PairingInfo {
  PairingInfo(this.server, this.code);
  final String server;
  final String code;

  /// Parses `songarr://pair?server=<url>&code=<code>`.
  static PairingInfo? fromQr(String raw) {
    final uri = Uri.tryParse(raw.trim());
    if (uri == null || uri.scheme != 'songarr' || uri.host != 'pair') return null;
    final server = uri.queryParameters['server'] ?? '';
    final code = uri.queryParameters['code'] ?? '';
    if (code.isEmpty) return null;
    return PairingInfo(normalizeServer(server), code);
  }

  static String normalizeServer(String s) {
    var v = s.trim();
    if (v.isEmpty) return v;
    if (!v.startsWith('http://') && !v.startsWith('https://')) v = 'https://$v';
    while (v.endsWith('/')) {
      v = v.substring(0, v.length - 1);
    }
    return v;
  }
}

class PairResult {
  PairResult(this.token, this.userId, this.userName);
  final String token;
  final int userId;
  final String userName;
}

/// Client for the Songarr app API (`/api/v1`). Every call carries the device token.
class SongarrApi {
  SongarrApi({required this.server, required this.token, this.alternatives = const [], this.onSwitch})
      : _dio = Dio(BaseOptions(
          baseUrl: '$server/api/v1',
          headers: {'Authorization': 'Bearer $token'},
          connectTimeout: const Duration(seconds: 15),
          receiveTimeout: const Duration(seconds: 30),
        ));

  final String server;
  final String token;
  final Dio _dio;

  /// The family's other servers (a backup that takes over when this one goes away).
  final List<String> alternatives;

  /// Told when another server answered instead: the app uses it from then on.
  final void Function(String server)? onSwitch;

  Map<String, String> get authHeaders => {'Authorization': 'Bearer $token'};
  Uri streamUri(String trackId) => Uri.parse('$server/api/v1/stream/$trackId');
  Uri episodeStreamUri(String episodeId) => Uri.parse('$server/api/v1/podcasts/episodes/$episodeId/stream');
  Uri appUpdateUri(String abi) => Uri.parse('$server/api/v1/app/update/apk').replace(queryParameters: {'abi': abi});

  /// A 30-second preview of a song that isn't on the server yet.
  Uri previewUri(CatalogItem item) => Uri.parse('$server/api/v1/preview').replace(queryParameters: {
        'source': item.source,
        'id': item.id,
        if (item.json['isrc'] != null) 'isrc': item.json['isrc'].toString(),
        if (item.artists.isNotEmpty) 'artist': item.artists.first,
        'title': item.title,
      });

  static Future<PairResult> pair(String server, String code, String device) async {
    final dio = Dio(BaseOptions(baseUrl: '$server/api/v1', connectTimeout: const Duration(seconds: 15)));
    final data = await _guard(() => dio.post('/auth/pair', data: {'code': code, 'device': device}));
    final user = data['user'] as Map<String, dynamic>;
    return PairResult(data['token'] as String, (user['id'] as num).toInt(), user['name'] as String);
  }

  /// Sign in with a profile's sign-in name and password (set on the PC, or in an app already signed in).
  static Future<PairResult> signIn(String server, String login, String password, String device) async {
    final dio = Dio(BaseOptions(baseUrl: '$server/api/v1', connectTimeout: const Duration(seconds: 15)));
    final data = await _guard(() => dio.post('/auth/login', data: {'login': login, 'password': password, 'device': device}));
    final user = data['user'] as Map<String, dynamic>;
    return PairResult(data['token'] as String, (user['id'] as num).toInt(), user['name'] as String);
  }

  static Future<dynamic> _guard(Future<Response> Function() call) async {
    try {
      return (await call()).data;
    } on DioException catch (e) {
      final body = e.response?.data;
      final code = e.response?.statusCode;
      final away = e.type == DioExceptionType.connectionError || e.type == DioExceptionType.connectionTimeout ||
          const [502, 503, 504, 530].contains(code); // 530: Cloudflare can't reach the server
      final msg = body is Map && body['error'] != null
          ? body['error'].toString()
          : switch (e.type) {
              DioExceptionType.connectionTimeout || DioExceptionType.receiveTimeout => 'The server didn\'t answer in time.',
              DioExceptionType.connectionError => 'Can\'t reach the ${Brand.current} server.',
              _ => 'Server error (${e.response?.statusCode ?? e.message}).',
            };
      throw ApiException(msg, status: code, away: away, active: body is Map ? body['active'] as String? : null);
    }
  }

  Future<Map<String, dynamic>> _get(String path, [Map<String, dynamic>? query]) async =>
      await _call((dio) => dio.get(path, queryParameters: query)) as Map<String, dynamic>;

  Future<Map<String, dynamic>> _send(String method, String path, [Object? body]) async =>
      (await _call((dio) => dio.request(path, data: body, options: Options(method: method))) as Map<String, dynamic>?) ?? {};

  /// A call to this server; if it's away (or standing by), the same call to whichever of the
  /// family's servers answers, which the app then keeps using.
  Future<dynamic> _call(Future<Response> Function(Dio dio) call) async {
    try {
      return await _guard(() => call(_dio));
    } on ApiException catch (e) {
      if (!e.away || alternatives.isEmpty) rethrow;
      final other = await findServer(prefer: e.active);
      if (other == null) rethrow;
      return await _guard(() => call(_dioFor(other)));
    }
  }

  Dio _dioFor(String base) => Dio(BaseOptions(
        baseUrl: '$base/api/v1',
        headers: authHeaders,
        connectTimeout: const Duration(seconds: 15),
        receiveTimeout: const Duration(seconds: 30),
      ));

  /// The first of the other servers that answers (not one standing by); the app switches to it.
  Future<String?> findServer({String? prefer}) async {
    final order = <String>[?prefer, ...alternatives].where((a) => a != server).toSet();
    for (final base in order) {
      try {
        final r = await Dio(BaseOptions(
          baseUrl: '$base/api/v1',
          headers: authHeaders,
          connectTimeout: const Duration(seconds: 6),
          receiveTimeout: const Duration(seconds: 10),
        )).get<Map<String, dynamic>>('/me');
        if (r.statusCode == 200) {
          onSwitch?.call(base);
          return base;
        }
      } catch (_) {
        // away too, or standing by: try the next
      }
    }
    return null;
  }

  List<Track> _tracks(dynamic list) => (list as List).map((t) => Track.fromJson(t as Map<String, dynamic>)).toList();

  // -- account ---------------------------------------------------------------

  Future<Me> me() async {
    final j = await _get('/me');
    final user = j['user'] as Map<String, dynamic>;
    return Me((user['id'] as num).toInt(), user['name'] as String,
        [for (final a in j['spotify_accounts'] as List) (a['name'] ?? '').toString()], j['server']['version'] as String,
        j['server'] as Map<String, dynamic>);
  }

  /// The icon chosen on the server, as a PNG at least [size] pixels square.
  Future<Uint8List> brandIcon(int size) async {
    final r = await _call((dio) => dio.get<List<int>>('/branding/icon',
        queryParameters: {'size': size}, options: Options(responseType: ResponseType.bytes)));
    return Uint8List.fromList(r as List<int>);
  }

  Future<void> logout() => _send('POST', '/auth/logout');

  Future<Account> account() async => Account(await _get('/account'));

  /// Set or change this profile's password ([current] is needed to change an existing one).
  Future<Account> setPassword(String login, String password, {String current = ''}) async =>
      Account(await _send('POST', '/account/password', {'login': login, 'password': password, 'current': current}));

  // -- browsing ----------------------------------------------------------------

  Future<Home> home() async => Home.fromJson(await _get('/home'));

  Future<SearchResults> search(String q) async {
    final j = await _get('/search', {'q': q, 'limit': 50});
    return SearchResults(
      _tracks(j['tracks']),
      [for (final a in j['albums'] as List) AlbumSummary(a as Map<String, dynamic>)],
      [for (final a in j['artists'] as List) ArtistSummary(a as Map<String, dynamic>)],
      j['top_artist'] == null ? null : ArtistRec(j['top_artist'] as Map<String, dynamic>),
    );
  }

  Future<Track> track(String id) async => Track.fromJson(await _get('/tracks/$id'));

  Future<TrackPage> album(String id) async => TrackPage.fromJson(await _get('/albums/$id'));

  Future<ArtistPage> artist(String name) async => ArtistPage.fromJson(await _get('/artist', {'name': name}));

  // -- following artists and their new releases ------------------------------------------------

  Future<void> followArtist(String name, int? deezerId) =>
      _send('POST', '/artists/follow', {'name': name, 'deezer_id': ?deezerId});

  Future<void> unfollowArtist(int deezerId) => _send('POST', '/artists/unfollow', {'deezer_id': deezerId});

  Future<List<FollowedArtist>> followingArtists() async =>
      [for (final a in (await _get('/artists/following'))['artists'] as List) FollowedArtist(a as Map<String, dynamic>)];

  Future<Notifications> notifications() async {
    final j = await _get('/notifications');
    return Notifications([for (final n in j['notifications'] as List) AppNotification(n as Map<String, dynamic>)],
        (j['unread'] as num?)?.toInt() ?? 0);
  }

  Future<void> markNotificationsRead() => _send('POST', '/notifications/read', {});

  /// (story or null, still being looked up: ask again shortly).
  Future<(ArtistAbout?, bool)> artistAbout(String name) async {
    final j = await _get('/artist/about', {'name': name});
    return (j['about'] == null ? null : ArtistAbout(j['about'] as Map<String, dynamic>), j['pending'] == true);
  }

  /// Null when there are no lyrics for the song.
  Future<Lyrics?> lyrics(String trackId) async {
    try {
      return Lyrics(await _get('/lyrics/$trackId'));
    } on ApiException catch (e) {
      if (e.status == 404) return null;
      rethrow;
    }
  }

  Future<LibraryInfo> library() async => _libraryInfo(await _get('/library'));

  /// Arrange your playlists: [ids] in the order you want them (kept on the server, for all your devices).
  Future<LibraryInfo> setPlaylistOrder(List<String> ids) async =>
      _libraryInfo(await _send('PUT', '/library/order', {'playlist_ids': ids}));

  static LibraryInfo _libraryInfo(Map<String, dynamic> j) => LibraryInfo((j['liked']['count'] as num).toInt(),
      [for (final p in j['playlists'] as List) PlaylistSummary(p as Map<String, dynamic>)]);

  /// All your Liked Songs, in your order.
  Future<TrackPage> liked() => _whole('/library/liked');

  /// A whole playlist.
  Future<TrackPage> playlist(String id) => _whole('/playlists/$id');

  /// Every song of a list, fetched 500 at a time (Liked Songs can run to thousands).
  Future<TrackPage> _whole(String path) async {
    final first = TrackPage.fromJson(await _get(path, {'offset': 0, 'limit': 500}));
    final tracks = [...first.tracks];
    while (tracks.length < first.total) {
      final next = TrackPage.fromJson(await _get(path, {'offset': tracks.length, 'limit': 500}));
      if (next.tracks.isEmpty) break;
      tracks.addAll(next.tracks);
    }
    return TrackPage(total: first.total, tracks: tracks, info: first.info);
  }

  /// Move the song at [from] in your Liked Songs to [to] (409 if the list changed meanwhile).
  Future<void> moveInLiked(int from, int to, String trackId) =>
      _send('POST', '/library/liked/move', {'from': from, 'to': to, 'track_id': trackId});

  // -- playlists made in the app -------------------------------------------------

  /// Move the song at position [from] to [to]. [trackId] is the song you mean (409 if the
  /// playlist changed elsewhere in the meantime).
  Future<void> moveInPlaylist(String id, int from, int to, String trackId) =>
      _send('POST', '/playlists/$id/tracks/move', {'from': from, 'to': to, 'track_id': trackId});

  Future<TrackPage> createPlaylist(String name) async => TrackPage.fromJson(await _send('POST', '/playlists', {'name': name}));
  Future<void> renamePlaylist(String id, String name) => _send('PATCH', '/playlists/$id', {'name': name});
  Future<void> deletePlaylist(String id) => _send('DELETE', '/playlists/$id');
  Future<void> addToPlaylist(String id, List<String> trackIds) => _send('POST', '/playlists/$id/tracks', {'track_ids': trackIds});
  Future<void> removeFromPlaylist(String id, int position) => _send('DELETE', '/playlists/$id/tracks/$position');

  // -- likes and plays -----------------------------------------------------------

  /// Returns whether the song is liked afterwards (it stays liked if it's liked on Spotify).
  Future<bool> setLiked(String trackId, bool liked) async =>
      (await _send(liked ? 'PUT' : 'DELETE', '/likes/$trackId'))['liked'] == true;

  Future<void> reportPlay(String trackId, Duration played, bool completed) =>
      _send('POST', '/plays', {'track_id': trackId, 'ms_played': played.inMilliseconds, 'completed': completed});

  // -- finding and requesting new songs -----------------------------------------------

  Future<(String?, List<CatalogItem>)> catalogSearch(String q) async {
    final j = await _get('/catalog/search', {'q': q});
    return (j['source'] as String?, [for (final r in j['results'] as List) CatalogItem(r as Map<String, dynamic>)]);
  }

  /// Albums anywhere (soundtracks, film scores…), to add whole.
  Future<List<CatalogAlbum>> catalogAlbums(String q) async {
    final j = await _get('/catalog/albums', {'q': q});
    return [for (final a in j['albums'] as List) CatalogAlbum(a as Map<String, dynamic>)];
  }

  /// One album's full track list, and which songs are already on the server.
  Future<CatalogAlbumPage> catalogAlbum(String source, String id) async => CatalogAlbumPage(await _get('/catalog/albums/$source/$id'));

  /// Add a whole album: the songs not on the server download next, in album order.
  Future<CatalogAlbumPage> requestAlbum(String source, String id) async =>
      CatalogAlbumPage(await _send('POST', '/requests/album', {'source': source, 'id': id}));

  /// Ask the server to download a song; [like] also saves it to Liked Songs.
  Future<Track> request(CatalogItem item, {bool like = false}) async =>
      Track.fromJson(await _send('POST', '/requests', {'source': item.source, 'id': item.json['id'], if (like) 'like': true}));

  Future<List<Track>> requests() async => _tracks((await _get('/requests'))['requests']);

  Future<List<Genre>> genres() async => [
        for (final g in (await _get('/discover/genres'))['genres'] as List)
          Genre((g['id'] as num).toInt(), g['name'] as String, g['image'] as String?),
      ];

  Future<List<CatalogItem>> genreChart(int id) async =>
      [for (final t in (await _get('/discover/genres/$id', {'limit': 50}))['tracks'] as List) CatalogItem(t as Map<String, dynamic>)];

  // -- Jams --------------------------------------------------------------------------

  Future<List<Person>> people() async => [for (final p in (await _get('/people'))['people'] as List) Person.fromJson(p as Map)];

  static List<Map<String, String>> jamItems(List<Track> tracks) =>
      [for (final t in tracks) t.isEpisode ? {'episode_id': t.id} : {'track_id': t.id}];

  Future<Jam> startJam(List<Track> tracks, int index, Duration position, bool playing, List<int> invite) async =>
      Jam(await _send('POST', '/jams', {
        'items': jamItems(tracks),
        'index': index,
        'position_ms': position.inMilliseconds,
        'playing': playing,
        'invite': invite,
      }));

  /// (the Jam I'm in, Jams I'm invited to, the server's clock in ms).
  Future<(Jam?, List<JamInvite>, int)> currentJam() async {
    final j = await _get('/jams/current');
    return (
      j['jam'] == null ? null : Jam(j['jam'] as Map<String, dynamic>),
      [for (final i in j['invites'] as List? ?? const []) JamInvite(i as Map<String, dynamic>)],
      (j['server_time'] as num?)?.toInt() ?? 0,
    );
  }

  Future<Jam> jam(String id) async => Jam(await _get('/jams/$id'));

  /// Waits (up to ~25 s) for the Jam to move past [version].
  Future<Jam> waitForJam(String id, int version) async => Jam(await _call((dio) => dio.get('/jams/$id',
          queryParameters: {'v': version, 'wait': 25}, options: Options(receiveTimeout: const Duration(seconds: 45)))) as Map<String, dynamic>);

  Future<Jam> joinJam(String id) async => Jam(await _send('POST', '/jams/$id/join'));
  Future<void> declineJam(String id) => _send('POST', '/jams/$id/decline');
  Future<void> leaveJam(String id) => _send('POST', '/jams/$id/leave');
  Future<Jam> inviteToJam(String id, List<int> people) async => Jam(await _send('POST', '/jams/$id/invite', {'invite': people}));
  Future<Jam> controlJam(String id, String action, [Map<String, dynamic> extra = const {}]) async =>
      Jam(await _send('POST', '/jams/$id/control', {'action': action, ...extra}));

  /// Register this phone for push notifications (Jam invites when the app is closed).
  Future<void> setPushToken(String token) => _send('POST', '/devices/push', {'token': token});

  // -- app updates --------------------------------------------------------------------

  /// A newer build for this phone's processor type, or null when this one is current.
  Future<AppUpdate?> appUpdate(String abi, int build) async {
    final j = await _get('/app/update', {'abi': abi, 'build': build});
    return j['available'] == true ? AppUpdate(j['update'] as Map<String, dynamic>) : null;
  }

  // -- podcasts -----------------------------------------------------------------------

  Future<PodcastShelves> podcasts() async => PodcastShelves.fromJson(await _get('/podcasts'));

  Future<PodcastDetail> podcast(String id) async => PodcastDetail.fromJson(await _get('/podcasts/$id'));

  Future<List<PodcastSummary>> searchPodcasts(String q) async =>
      [for (final p in (await _get('/podcasts/search', {'q': q}))['results'] as List) PodcastSummary(p as Map<String, dynamic>)];

  /// Returns whether the show is followed afterwards (it stays followed while it's saved on Spotify).
  Future<bool> setFollowing(String podcastId, bool follow) async =>
      (await _send(follow ? 'PUT' : 'DELETE', '/podcasts/$podcastId/follow'))['following'] == true;

  /// Download a show's new episodes: 'off', until 'played', or for a number of days.
  Future<String> setPodcastDownloads(String podcastId, String keep) async =>
      (await _send('PUT', '/podcasts/$podcastId/downloads', {'keep': keep}))['downloads'] as String;

  Future<void> keepEpisode(String episodeId, String keep) => _send('PUT', '/podcasts/episodes/$episodeId/download', {'keep': keep});
  Future<void> dropEpisode(String episodeId) => _send('DELETE', '/podcasts/episodes/$episodeId/download');

  /// Episodes this profile keeps downloaded (the server's copies are mirrored onto the device).
  Future<List<Episode>> podcastDownloads() async =>
      [for (final e in (await _get('/podcasts/downloads'))['episodes'] as List) Episode(e as Map<String, dynamic>)];

  Future<void> saveEpisodeProgress(String episodeId, Duration position, Duration? duration, bool completed) =>
      _send('POST', '/podcasts/episodes/$episodeId/progress', {
        'position_ms': position.inMilliseconds,
        'duration_ms': duration?.inMilliseconds,
        'completed': completed,
      });
}
