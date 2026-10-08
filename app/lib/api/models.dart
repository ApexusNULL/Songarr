import 'dart:convert' show base64Decode, base64Encode;
import 'dart:typed_data';

import 'package:flutter/material.dart' show DateUtils;

// Data types of the Songarr app API (see docs/API.md in the server project).

List<String> _strings(dynamic v) => (v as List? ?? const []).map((e) => e.toString()).toList();

class Track {
  Track(this.json);

  /// The server's JSON, kept as-is so tracks can be stored for offline use.
  final Map<String, dynamic> json;

  factory Track.fromJson(Map<String, dynamic> j) => Track(Map<String, dynamic>.from(j));

  String get id => json['id'] as String;
  String get title => json['title'] as String? ?? '';
  List<String> get artists => _strings(json['artists']);
  String get artistLine => artists.join(', ');
  String? get album => json['album'] as String?;
  String? get albumId => json['album_id'] as String?;
  List<String> get albumArtists => _strings(json['album_artists']);
  String? get releaseDate => json['release_date'] as String?;
  int? get trackNumber => json['track_number'] as int?;
  int? get discNumber => json['disc_number'] as int?;
  Duration get duration => Duration(milliseconds: (json['duration_ms'] as num?)?.toInt() ?? 0);
  bool get explicit => json['explicit'] == true;
  String? get coverUrl => json['cover_url'] as String?;
  String? get thumbUrl => (json['thumb_url'] as String?) ?? coverUrl;
  String get status => json['status'] as String? ?? 'wanted';
  bool get playable => json['playable'] == true;
  String get format => json['format'] as String? ?? 'm4a';
  int? get size => (json['size'] as num?)?.toInt();
  bool get liked => json['liked'] == true;

  /// Podcast episodes play through the same player as songs.
  bool get isEpisode => json['kind'] == 'episode';
  bool get isPreview => json['kind'] == 'preview';
  String? get podcastId => json['podcast_id'] as String?;
  Duration get progress => Duration(milliseconds: (json['progress_ms'] as num?)?.toInt() ?? 0);
  bool get completed => json['completed'] == true;

  Track copyWith({bool? liked}) => Track({...json, 'liked': ?liked});

  /// A human description of where a not-yet-playable song is.
  String get statusText => switch (status) {
        'downloaded' => 'Ready',
        'searching' || 'downloading' => 'Downloading to server…',
        'review' => 'Waiting for a match to be picked on the server',
        'failed' => 'Server couldn\'t download this yet',
        'ignored' => 'Ignored on the server',
        _ => 'Queued on the server',
      };
}

class PlaylistSummary {
  PlaylistSummary(this.json);
  final Map<String, dynamic> json;

  String get id => json['id'] as String;
  String get name => json['name'] as String? ?? '';
  String get kind => json['kind'] as String? ?? 'app';
  String? get owner => json['owner'] as String?;
  String? get imageUrl => json['image_url'] as String?;
  int get count => (json['count'] as num?)?.toInt() ?? 0;
  bool get editable => json['editable'] == true;

  /// Brought over from Spotify (now your own playlist; it keeps up with Spotify while you move over).
  bool get fromSpotify => json['from_spotify'] == true;

  String get origin => kind == 'spotify' ? (owner ?? 'Spotify') : fromSpotify ? 'from Spotify' : 'made in ${Brand.current}';
}

class TrackPage {
  TrackPage({required this.total, required this.tracks, this.info = const {}});
  final int total;
  final List<Track> tracks;
  final Map<String, dynamic> info;

  factory TrackPage.fromJson(Map<String, dynamic> j) => TrackPage(
        total: (j['total'] as num?)?.toInt() ?? (j['tracks'] as List).length,
        tracks: (j['tracks'] as List).map((t) => Track.fromJson(t as Map<String, dynamic>)).toList(),
        info: j,
      );

  String get name => info['name'] as String? ?? '';
  String? get imageUrl => (info['image_url'] ?? info['cover_url']) as String?;
  bool get editable => info['editable'] == true;
}

class AlbumSummary {
  AlbumSummary(this.json);
  final Map<String, dynamic> json;
  String get id => json['id'] as String;
  String get name => json['name'] as String? ?? '';
  String? get coverUrl => json['cover_url'] as String?;
  String get year => json['year'] as String? ?? '';
  List<String> get artists => _strings(json['artists']);
}

class ArtistSummary {
  ArtistSummary(this.json);
  final Map<String, dynamic> json;
  String get name => json['name'] as String? ?? '';
  String? get coverUrl => json['cover_url'] as String?;
  int get count => (json['count'] as num?)?.toInt() ?? 0;
}

class HomeSection {
  HomeSection(this.id, this.title, this.tracks);
  final String id;
  final String title;
  final List<Track> tracks;
}

class Home {
  Home(this.sections, this.playlists,
      {this.likedCount = 0, this.forYou, PodcastShelves? podcasts, this.releases = const [], this.unreadNotifications = 0})
      : podcasts = podcasts ?? PodcastShelves.empty;
  final List<HomeSection> sections;
  final List<PlaylistSummary> playlists;
  final int likedCount;

  /// Recent releases from the artists you follow, newest first.
  final List<CatalogAlbum> releases;
  final int unreadNotifications;

  /// Made-for-you picks; null while the server is still preparing them.
  final ForYou? forYou;
  final PodcastShelves podcasts;

  HomeSection? section(String id) {
    for (final s in sections) {
      if (s.id == id) return s;
    }
    return null;
  }

  factory Home.fromJson(Map<String, dynamic> j) => Home(
        [
          for (final s in j['sections'] as List)
            HomeSection(s['id'] as String, s['title'] as String,
                (s['tracks'] as List).map((t) => Track.fromJson(t as Map<String, dynamic>)).toList()),
        ],
        [for (final p in j['playlists'] as List? ?? const []) PlaylistSummary(p as Map<String, dynamic>)],
        likedCount: (j['liked_count'] as num?)?.toInt() ?? 0,
        forYou: j['for_you'] == null ? null : ForYou.fromJson(j['for_you'] as Map<String, dynamic>),
        podcasts: j['podcasts'] == null ? null : PodcastShelves.fromJson(j['podcasts'] as Map<String, dynamic>),
        releases: [for (final a in j['releases'] as List? ?? const []) CatalogAlbum(a as Map<String, dynamic>)],
        unreadNotifications: (j['unread_notifications'] as num?)?.toInt() ?? 0,
      );
}

/// A recommended song: playable when [track] is set and downloaded, otherwise it can be added.
class RecSong {
  RecSong(this.json);
  final Map<String, dynamic> json;

  String get title => json['title'] as String? ?? '';
  List<String> get artists => _strings(json['artists']);
  String get artistLine => artists.join(', ');
  String? get coverUrl => (json['cover_url'] ?? json['thumb_url']) as String?;
  String? get reason => json['reason'] as String?;
  Track? get track => json['track'] == null ? null : Track.fromJson(json['track'] as Map<String, dynamic>);
  bool get playable => track?.playable == true;
  bool get onServer => json['in_library'] != null;

  /// For "Add": the request endpoint takes the same source/id as search results.
  CatalogItem get catalogItem => CatalogItem(json);
}

class ArtistRec {
  ArtistRec(this.json);
  final Map<String, dynamic> json;
  String get name => json['name'] as String? ?? '';
  String? get imageUrl => (json['image_url'] ?? json['thumb_url']) as String?;
  String? get thumbUrl => (json['thumb_url'] ?? json['image_url']) as String?;
  String? get reason => json['reason'] as String?;
  int get songsOnServer => (json['songs_on_server'] as num?)?.toInt() ?? 0;
  int? get fans => (json['fans'] as num?)?.toInt();
}

class ForYou {
  ForYou(this.songs, this.artists, this.topArtists, this.podcasts);
  final List<RecSong> songs;
  final List<ArtistRec> artists;
  final List<ArtistRec> topArtists;
  final List<PodcastSummary> podcasts;

  factory ForYou.fromJson(Map<String, dynamic> j) => ForYou(
        [for (final s in j['songs'] as List? ?? const []) RecSong(s as Map<String, dynamic>)],
        [for (final a in j['artists'] as List? ?? const []) ArtistRec(a as Map<String, dynamic>)],
        [for (final a in j['top_artists'] as List? ?? const []) ArtistRec(a as Map<String, dynamic>)],
        [for (final p in j['podcasts'] as List? ?? const []) PodcastSummary(p as Map<String, dynamic>)],
      );
}

/// GET /artist: songs on the server plus, when Deezer knows the artist, their popular songs.
class ArtistPage {
  ArtistPage(this.name, this.tracks, this.albums, this.about, this.popular, this.related, {this.deezerId, this.following = false});
  final String name;
  final List<Track> tracks;
  final List<AlbumSummary> albums;
  final ArtistRec? about;
  final List<CatalogItem> popular;
  final List<ArtistRec> related;
  final int? deezerId; // null when the music catalogue doesn't know them (they can't be followed)
  final bool following;

  factory ArtistPage.fromJson(Map<String, dynamic> j) => ArtistPage(
        j['name'] as String? ?? '',
        (j['tracks'] as List).map((t) => Track.fromJson(t as Map<String, dynamic>)).toList(),
        [for (final a in j['albums'] as List? ?? const []) AlbumSummary(a as Map<String, dynamic>)],
        j['about'] == null ? null : ArtistRec(j['about'] as Map<String, dynamic>),
        [for (final t in j['popular'] as List? ?? const []) CatalogItem(t as Map<String, dynamic>)],
        [for (final a in j['related'] as List? ?? const []) ArtistRec(a as Map<String, dynamic>)],
        deezerId: (j['deezer_id'] as num?)?.toInt(),
        following: j['following'] == true,
      );
}

/// An artist you follow, with their latest release.
class FollowedArtist {
  FollowedArtist(this.json);
  final Map<String, dynamic> json;
  int get deezerId => (json['deezer_id'] as num).toInt();
  String get name => json['name'] as String? ?? '';
  String? get imageUrl => json['image_url'] as String?;
  Map<String, dynamic>? get latest => json['latest'] as Map<String, dynamic>?;
  String? get latestName => latest?['name'] as String?;
  String? get latestDate => latest?['release_date'] as String?;
}

/// Something to tell you about (the bell on Home): a new release from an artist you follow.
class AppNotification {
  AppNotification(this.json);
  final Map<String, dynamic> json;
  int get id => (json['id'] as num).toInt();
  String get kind => json['kind'] as String? ?? '';
  String get title => json['title'] as String? ?? '';
  String get body => json['body'] as String? ?? '';
  double get created => (json['created'] as num?)?.toDouble() ?? 0;
  bool get read => json['read'] == true;
  CatalogAlbum? get album => json['album'] == null ? null : CatalogAlbum(json['album'] as Map<String, dynamic>);
}

class Notifications {
  Notifications(this.items, this.unread);
  final List<AppNotification> items;
  final int unread;
}

/// "Out today", "Out yesterday", "3 days ago", "2 weeks ago", "Out 12 Oct" (coming), or the date.
String releaseAge(String? isoDate, [DateTime? now]) {
  final d = isoDate == null ? null : DateTime.tryParse(isoDate);
  if (d == null) return '';
  final today = DateUtils.dateOnly(now ?? DateTime.now());
  final days = today.difference(DateUtils.dateOnly(d)).inDays;
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  final date = '${d.day} ${months[d.month - 1]}';
  if (days < 0) return 'Out $date';
  if (days == 0) return 'Out today';
  if (days == 1) return 'Out yesterday';
  if (days < 14) return '$days days ago';
  if (days < 60) return '${days ~/ 7} weeks ago';
  return days < 365 ? date : '$date ${d.year}';
}

/// "just now", "5 min ago", "3 h ago", "2 days ago" for a Unix time in seconds.
String timeAgo(double epochSeconds, [DateTime? now]) {
  if (epochSeconds <= 0) return '';
  final s = ((now ?? DateTime.now()).millisecondsSinceEpoch / 1000 - epochSeconds).round();
  if (s < 60) return 'just now';
  if (s < 3600) return '${s ~/ 60} min ago';
  if (s < 86400) return '${s ~/ 3600} h ago';
  final days = s ~/ 86400;
  return days == 1 ? 'yesterday' : '$days days ago';
}

// -- podcasts -------------------------------------------------------------------------

class PodcastSummary {
  PodcastSummary(this.json);
  final Map<String, dynamic> json;
  String get id => json['id'] as String;
  String get title => json['title'] as String? ?? '';
  String? get author => json['author'] as String?;
  String? get artworkUrl => json['artwork_url'] as String?;
  String? get genre => json['genre'] as String?;
  String? get reason => json['reason'] as String?;
  bool get following => json['following'] == true;
}

class Episode {
  Episode(this.json);
  final Map<String, dynamic> json;

  String get id => json['id'] as String;
  String get podcastId => json['podcast_id'] as String;
  String get podcastTitle => json['podcast_title'] as String? ?? '';
  String get title => json['title'] as String? ?? '';
  String? get description => json['description'] as String?;
  String? get imageUrl => json['image_url'] as String?;
  Duration get duration => Duration(milliseconds: (json['duration_ms'] as num?)?.toInt() ?? 0);
  Duration get progress => Duration(milliseconds: (json['progress_ms'] as num?)?.toInt() ?? 0);
  bool get completed => json['completed'] == true;
  DateTime? get published {
    final p = json['published'] as num?;
    return p == null ? null : DateTime.fromMillisecondsSinceEpoch((p * 1000).round());
  }

  /// How this profile keeps the episode downloaded: 'played', a number of days, or null.
  String? get keep => json['keep'] as String?;
  DateTime? get expires {
    final e = json['expires'] as num?;
    return e == null ? null : DateTime.fromMillisecondsSinceEpoch((e * 1000).round());
  }

  bool get onServer => json['on_server'] == true;
  int? get size => (json['size'] as num?)?.toInt();

  /// "until played" / "5 days left", for kept episodes.
  String? get keepLabel {
    if (keep == null) return null;
    if (keep == 'played') return 'until played';
    final left = expires?.difference(DateTime.now());
    if (left == null) return 'for $keep days';
    if (left.inHours < 24) return 'until tomorrow';
    return '${left.inDays + 1} days left';
  }

  /// 0..1 through the episode, for progress bars.
  double get fraction =>
      completed ? 1 : (duration.inMilliseconds == 0 ? 0 : (progress.inMilliseconds / duration.inMilliseconds).clamp(0.0, 1.0));

  Track toTrack() => Track({
        'id': id,
        'kind': 'episode',
        'podcast_id': podcastId,
        'title': title,
        'artists': [podcastTitle],
        'album': podcastTitle,
        'duration_ms': json['duration_ms'],
        'cover_url': imageUrl,
        'thumb_url': imageUrl,
        'status': 'downloaded',
        'playable': true,
        'progress_ms': json['progress_ms'],
        'completed': completed,
      });
}

class PodcastShelves {
  PodcastShelves(this.following, this.continueListening, this.newEpisodes, this.recommended);
  static final empty = PodcastShelves(const [], const [], const [], const []);
  final List<PodcastSummary> following;
  final List<Episode> continueListening;
  final List<Episode> newEpisodes;
  final List<PodcastSummary> recommended;

  factory PodcastShelves.fromJson(Map<String, dynamic> j) => PodcastShelves(
        [for (final p in j['following'] as List? ?? const []) PodcastSummary(p as Map<String, dynamic>)],
        [for (final e in j['continue'] as List? ?? const []) Episode(e as Map<String, dynamic>)],
        [for (final e in j['new_episodes'] as List? ?? const []) Episode(e as Map<String, dynamic>)],
        [for (final p in j['recommended'] as List? ?? const []) PodcastSummary(p as Map<String, dynamic>)],
      );
}

class PodcastDetail {
  PodcastDetail(this.json, this.episodes);
  final Map<String, dynamic> json;
  final List<Episode> episodes;

  PodcastSummary get summary => PodcastSummary(json);
  String? get description => json['description'] as String?;
  bool get onSpotify => json['on_spotify'] == true;
  bool get stale => json['stale'] == true;

  /// The show's download setting: 'off', 'played' or a number of days.
  String get downloads => json['downloads'] as String? ?? 'off';

  factory PodcastDetail.fromJson(Map<String, dynamic> j) =>
      PodcastDetail(j, [for (final e in j['episodes'] as List? ?? const []) Episode(e as Map<String, dynamic>)]);
}

class LibraryInfo {
  LibraryInfo(this.likedCount, this.playlists);
  final int likedCount;
  final List<PlaylistSummary> playlists;
}

class SearchResults {
  SearchResults(this.tracks, this.albums, this.artists, [this.topArtist]);
  final List<Track> tracks;
  final List<AlbumSummary> albums;
  final List<ArtistSummary> artists;

  /// The artist the search names, shown first with their bio (may have no songs here yet).
  final ArtistRec? topArtist;
}

/// A song from Spotify/Deezer search or a genre chart; may not be in the library yet.
class CatalogItem {
  CatalogItem(this.json);
  final Map<String, dynamic> json;

  String get source => json['source'] as String;
  String get id => json['id'].toString();
  String get title => json['title'] as String? ?? '';
  List<String> get artists => _strings(json['artists']);
  String get artistLine => artists.join(', ');
  String? get album => json['album'] as String?;
  Duration get duration => Duration(milliseconds: (json['duration_ms'] as num?)?.toInt() ?? 0);
  bool get explicit => json['explicit'] == true;
  String? get thumbUrl => (json['thumb_url'] ?? json['cover_url']) as String?;
  String? get inLibrary => json['in_library'] as String?;
  String? get status => json['status'] as String?;
  String? get coverUrl => (json['cover_url'] ?? json['thumb_url']) as String?;

  // set on an album's track list
  int? get trackNumber => (json['track_number'] as num?)?.toInt();
  int get discNumber => (json['disc_number'] as num?)?.toInt() ?? 1;

  /// The server's copy, when it has the song.
  Track? get track => json['track'] == null ? null : Track.fromJson(json['track'] as Map<String, dynamic>);

  /// What the player plays for a preview: the clip, streamed through the server.
  Track previewTrack(Uri uri) => Track({
        'id': 'preview-$source-$id',
        'kind': 'preview',
        'preview_uri': uri.toString(),
        'title': title,
        'artists': artists,
        'album': album,
        'duration_ms': 30000,
        'cover_url': coverUrl,
        'thumb_url': thumbUrl,
        'status': 'downloaded',
        'playable': true,
      });
}

class Genre {
  Genre(this.id, this.name, this.image);
  final int id;
  final String name;
  final String? image;
}

class Me {
  Me(this.userId, this.userName, this.spotifyAccounts, this.serverVersion, [this.server = const {}]);
  final int userId;
  final String userName;
  final List<String> spotifyAccounts;
  final String serverVersion;
  final Map<String, dynamic> server; // name, version, icon (id or null), background, see_through
}

/// The name and icon chosen on the server (Settings → Name and icon).
class Brand {
  const Brand({this.name = builtIn, this.iconId, this.icon, this.seeThrough = true});

  /// The name this build was made with: the server passes it in when it rebuilds the app.
  static const builtIn = String.fromEnvironment('BRAND_NAME', defaultValue: 'Songarr');

  /// The name right now, for text made outside widgets (error messages).
  static String current = builtIn;

  final String name;
  final String? iconId;
  final Uint8List? icon; // PNG, 256 × 256
  final bool seeThrough; // a logo on nothing (shown on the gradient) rather than a full square picture

  factory Brand.fromJson(Map<String, dynamic> j) => Brand(
      name: j['name'] as String? ?? builtIn,
      iconId: j['icon'] as String?,
      icon: j['png'] == null ? null : base64Decode(j['png'] as String),
      seeThrough: j['see_through'] as bool? ?? true);

  Map<String, dynamic> toJson() =>
      {'name': name, 'icon': iconId, 'png': icon == null ? null : base64Encode(icon!), 'see_through': seeThrough};
}

/// A newer app build offered by the server (`GET /app/update`).
class AppUpdate {
  AppUpdate(this.json);
  final Map<String, dynamic> json;
  String get version => json['version'] as String? ?? '';
  int get build => (json['build'] as num?)?.toInt() ?? 0;
  String get notes => json['notes'] as String? ?? '';
  int get size => (json['size'] as num?)?.toInt() ?? 0;
  String get sha256 => json['sha256'] as String? ?? '';
  String get abi => json['abi'] as String? ?? '';
}

/// An artist's story (GET /artist/about): from Wikipedia, or Last.fm for smaller artists.
class ArtistAbout {
  ArtistAbout(this.json);
  final Map<String, dynamic> json;

  String? get description => json['description'] as String?;
  String get summary => json['summary'] as String? ?? '';
  String? get imageUrl => json['image_url'] as String?;
  String get source => json['source'] as String? ?? 'Wikipedia';
  String? get license => json['license'] as String?;
  Map<String, dynamic> get facts => (json['facts'] as Map?)?.cast<String, dynamic>() ?? const {};
  List<({String heading, String text})> get history => [
        for (final c in json['history'] as List? ?? const [])
          (heading: (c as Map)['heading'] as String? ?? '', text: c['text'] as String? ?? ''),
      ];

  List<String> _list(String key) => (facts[key] as List? ?? const []).map((e) => e.toString()).toList();
  List<String> get genres => _list('genres');
  List<String> get labels => _list('labels');
  List<String> get members => _list('members');
  bool get isGroup => facts['type'] == 'group';

  /// "Formed 2006 in Portland, United States", "Born 1991 · Bloomington, United States"…
  String? get origin {
    final year = isGroup ? facts['formed'] : facts['born'];
    final end = isGroup ? facts['ended'] : facts['died'];
    final place = facts['origin'] as String?;
    final parts = <String>[
      if (year != null) '${isGroup ? 'Formed' : 'Born'} $year${end != null ? (isGroup ? ' (disbanded $end)' : ' (died $end)') : ''}',
      ?place,
    ];
    return parts.isEmpty ? null : parts.join(' · ');
  }
}

/// A song's lyrics (GET /lyrics/{id}): timed lines when available, else plain text.
class Lyrics {
  Lyrics(this.json);
  final Map<String, dynamic> json;

  List<({Duration at, String text})> get synced => [
        for (final l in json['synced'] as List? ?? const [])
          (at: Duration(milliseconds: ((l as Map)['t'] as num).toInt()), text: l['text'] as String? ?? ''),
      ];
  bool get isSynced => (json['synced'] as List?)?.isNotEmpty == true;
  String get plain => json['plain'] as String? ?? '';
  bool get instrumental => json['instrumental'] == true;
  String get source => json['source'] as String? ?? 'LRCLIB';
}

// -- Jams: listening together ---------------------------------------------------------------

class Person {
  Person(this.id, this.name);
  final int id;
  final String name;
  factory Person.fromJson(Map j) => Person((j['id'] as num).toInt(), j['name'] as String? ?? '');
}

/// A Jam's shared timeline: "at server time [ref], [current] is at [position]".
class Jam {
  Jam(this.json);
  final Map<String, dynamic> json;

  String get id => json['id'] as String;
  Person get host => Person.fromJson(json['host'] as Map);
  List<Person> get members => [for (final m in json['members'] as List? ?? const []) Person.fromJson(m as Map)];
  List<Person> get invited => [for (final m in json['invited'] as List? ?? const []) Person.fromJson(m as Map)];
  List<Track> get queue => [for (final t in json['queue'] as List? ?? const []) Track.fromJson(t as Map<String, dynamic>)];
  int get index => (json['index'] as num?)?.toInt() ?? 0;
  bool get playing => json['playing'] == true;
  Duration get position => Duration(milliseconds: (json['position_ms'] as num?)?.toInt() ?? 0);
  int get ref => (json['ref'] as num?)?.toInt() ?? 0; // server clock, ms
  int get serverTime => (json['server_time'] as num?)?.toInt() ?? 0;
  int get version => (json['version'] as num?)?.toInt() ?? 0;
  bool get ended => json['ended'] == true;
  Track? get current {
    final q = queue;
    return index >= 0 && index < q.length ? q[index] : null;
  }

  /// Where the song is at server time [nowMs].
  Duration positionAt(int nowMs) => !playing || nowMs <= ref ? position : position + Duration(milliseconds: nowMs - ref);
}

class JamInvite {
  JamInvite(this.json);
  final Map<String, dynamic> json;
  String get id => json['id'] as String;
  String get from => json['from'] as String? ?? '';
  Track? get item => json['item'] == null ? null : Track.fromJson(json['item'] as Map<String, dynamic>);
}

/// How this profile signs in on other devices.
class Account {
  Account(this.json);
  final Map<String, dynamic> json;
  String get name => json['name'] as String? ?? '';
  String? get login => json['login'] as String?;
  bool get hasPassword => json['has_password'] == true;
}

/// An album from the catalogue (Deezer), to add whole: a soundtrack, a film score, a live album…
class CatalogAlbum {
  CatalogAlbum(this.json);
  final Map<String, dynamic> json;

  String get source => json['source'] as String? ?? 'deezer';
  String get id => json['id'].toString();
  String get name => json['name'] as String? ?? '';
  List<String> get artists => _strings(json['artists']);
  String get artistLine => artists.join(', ');
  String get type => json['type'] as String? ?? 'album';
  String get typeLabel => switch (type) { 'single' => 'Single', 'ep' => 'EP', 'compile' => 'Compilation', _ => 'Album' };
  int? get count => (json['count'] as num?)?.toInt();
  int get onServer => (json['on_server'] as num?)?.toInt() ?? 0;
  String? get coverUrl => (json['cover_url'] ?? json['thumb_url']) as String?;
  String? get thumbUrl => (json['thumb_url'] ?? json['cover_url']) as String?;
  String? get releaseDate => json['release_date'] as String?;
}

/// A catalogue album with its full track list, and which songs the server already has.
class CatalogAlbumPage extends CatalogAlbum {
  CatalogAlbumPage(super.json);

  String? get year => json['year'] as String?;
  String? get label => json['label'] as String?;
  @override
  int get count => (json['count'] as num?)?.toInt() ?? tracks.length;

  /// Every song is in your library (requested or liked), whether or not it's downloaded yet.
  bool get added => json['added'] == true;
  late final List<CatalogItem> tracks = [for (final t in json['tracks'] as List? ?? const []) CatalogItem(t as Map<String, dynamic>)];
  int get discs => tracks.isEmpty ? 1 : tracks.map((t) => t.discNumber).reduce((a, b) => a > b ? a : b);
  Duration get length => tracks.fold(Duration.zero, (s, t) => s + t.duration);

  String get subtitle => [
        typeLabel,
        ?year,
        '$count songs',
        if (length > Duration.zero) formatAlbumLength(length),
        if (onServer > 0) '$onServer on your server',
      ].join(' · ');
}

String formatAlbumLength(Duration d) => d.inHours > 0 ? '${d.inHours} h ${d.inMinutes % 60} min' : '${d.inMinutes} min';

/// One YouTube upload of a song (GET /tracks/{id}/versions), to replace a wrong download with.
class SongVersion {
  SongVersion(this.json);
  final Map<String, dynamic> json;
  String get youtubeId => json['youtube_id'] as String;
  String get title => json['title'] as String? ?? '';
  /// Who uploaded it ("Artist - Topic" channels are the label's own audio).
  String get channel => (json['channel'] as String? ?? '').replaceAll(RegExp(r' - Topic$'), '');
  Duration? get duration =>
      json['duration_s'] == null ? null : Duration(milliseconds: ((json['duration_s'] as num) * 1000).round());
  double get score => (json['score'] as num?)?.toDouble() ?? 0;
  bool get official => json['official'] == true;
  /// The one the song has now.
  bool get current => json['current'] == true;
  String get url => json['url'] as String? ?? 'https://youtu.be/$youtubeId';
}

class SongVersions {
  SongVersions(Map<String, dynamic> j)
      : current = j['current'] as String?,
        currentUrl = j['current_url'] as String?,
        replacing = j['replacing'] as String?,
        expected = j['expected_s'] == null ? null : Duration(milliseconds: ((j['expected_s'] as num) * 1000).round()),
        versions = [for (final v in j['versions'] as List) SongVersion(v as Map<String, dynamic>)];
  final String? current;
  final String? currentUrl;
  /// Another version on its way to replace it (the song plays the one it has until then).
  final String? replacing;
  /// How long the song should be.
  final Duration? expected;
  /// Best match first.
  final List<SongVersion> versions;
}
