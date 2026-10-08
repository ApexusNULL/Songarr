import 'package:flutter_riverpod/flutter_riverpod.dart';


import '../api/client.dart';
import '../api/models.dart';
import 'offline.dart';
import 'session.dart';

SongarrApi _api(Ref ref) {
  final api = ref.watch(apiProvider);
  if (api == null) throw ApiException('Not signed in.', status: 401);
  return api;
}

final homeProvider = FutureProvider.autoDispose<Home>((ref) => _api(ref).home());
final followingProvider = FutureProvider.autoDispose<List<FollowedArtist>>((ref) => _api(ref).followingArtists());
final notificationsProvider = FutureProvider.autoDispose<Notifications>((ref) => _api(ref).notifications());
final libraryProvider = FutureProvider.autoDispose<LibraryInfo>((ref) => _api(ref).library());
final likedProvider = FutureProvider.autoDispose<TrackPage>((ref) async => _fresh(ref, await _api(ref).liked()));
final playlistProvider =
    FutureProvider.autoDispose.family<TrackPage, String>((ref, id) async => _fresh(ref, await _api(ref).playlist(id)));

/// Downloads in [page] of songs replaced on the server since are fetched again.
TrackPage _fresh(Ref ref, TrackPage page) {
  ref.read(offlineProvider.notifier).refreshStale(page.tracks);
  return page;
}
final albumProvider = FutureProvider.autoDispose.family<TrackPage, String>((ref, id) => _api(ref).album(id));
final artistProvider = FutureProvider.autoDispose.family<ArtistPage, String>((ref, name) => _api(ref).artist(name));
final searchProvider = FutureProvider.autoDispose.family<SearchResults, String>((ref, q) => _api(ref).search(q));
final catalogProvider =
    FutureProvider.autoDispose.family<(String?, List<CatalogItem>), String>((ref, q) => _api(ref).catalogSearch(q));
final catalogAlbumsProvider = FutureProvider.autoDispose.family<List<CatalogAlbum>, String>((ref, q) => _api(ref).catalogAlbums(q));

/// Keyed by "source:id".
final catalogAlbumProvider = FutureProvider.autoDispose.family<CatalogAlbumPage, String>((ref, key) {
  final i = key.indexOf(':');
  return _api(ref).catalogAlbum(key.substring(0, i), key.substring(i + 1));
});
final genresProvider = FutureProvider<List<Genre>>((ref) => _api(ref).genres());
final genreChartProvider = FutureProvider.autoDispose.family<List<CatalogItem>, int>((ref, id) => _api(ref).genreChart(id));
final requestsProvider = FutureProvider.autoDispose<List<Track>>((ref) => _api(ref).requests());
final meProvider = FutureProvider.autoDispose<Me>((ref) => _api(ref).me());
final accountProvider = FutureProvider.autoDispose<Account>((ref) => _api(ref).account());
final podcastsProvider = FutureProvider.autoDispose<PodcastShelves>((ref) => _api(ref).podcasts());

/// An artist's story; the server may need a little while to look a new artist up.
final artistAboutProvider = FutureProvider.autoDispose.family<ArtistAbout?, String>((ref, name) async {
  final api = _api(ref);
  var alive = true;
  ref.onDispose(() => alive = false);
  for (var i = 0; i < 12 && alive; i++) {
    final (about, pending) = await api.artistAbout(name);
    if (!pending) return about;
    await Future<void>.delayed(const Duration(seconds: 5));
  }
  return null;
});

final lyricsProvider = FutureProvider.autoDispose.family<Lyrics?, String>((ref, trackId) => _api(ref).lyrics(trackId));
final podcastProvider = FutureProvider.autoDispose.family<PodcastDetail, String>((ref, id) => _api(ref).podcast(id));
final podcastSearchProvider =
    FutureProvider.autoDispose.family<List<PodcastSummary>, String>((ref, q) => _api(ref).searchPodcasts(q));

/// After a like, playlist edit or request, refresh every view that might show it.
extension RefreshLibrary on WidgetRef {
  void refreshLibrary() {
    for (final p in [homeProvider, libraryProvider, likedProvider, requestsProvider]) {
      invalidate(p);
    }
    for (final f in [playlistProvider, searchProvider, catalogProvider, genreChartProvider, albumProvider, artistProvider]) {
      invalidate(f);
    }
  }

  /// A new release, or the app coming back: Home's shelf and bell, the bell's list, and each
  /// followed artist's latest release.
  void refreshReleases() {
    invalidate(homeProvider);
    invalidate(notificationsProvider);
    invalidate(followingProvider);
  }

  /// After following a show or listening to an episode.
  void refreshPodcasts() {
    invalidate(homeProvider);
    invalidate(podcastsProvider);
    invalidate(podcastProvider);
    invalidate(podcastSearchProvider);
  }
}
