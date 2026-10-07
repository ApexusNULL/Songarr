import 'package:flutter_test/flutter_test.dart';
import 'package:songarr_app/api/models.dart';

void main() {
  final now = DateTime(2026, 10, 7, 15, 0);

  test('how long ago a release came out', () {
    expect(releaseAge('2026-10-07', now), 'Out today');
    expect(releaseAge('2026-10-06', now), 'Out yesterday');
    expect(releaseAge('2026-10-02', now), '5 days ago');
    expect(releaseAge('2026-09-16', now), '3 weeks ago');
    expect(releaseAge('2026-10-17', now), 'Out 17 Oct'); // coming soon
    expect(releaseAge('2026-03-01', now), '1 Mar');
    expect(releaseAge('2019-03-01', now), '1 Mar 2019');
    expect(releaseAge(null, now), '');
    expect(releaseAge('', now), '');
  });

  test('when a notification arrived', () {
    final t = now.millisecondsSinceEpoch / 1000;
    expect(timeAgo(t - 20, now), 'just now');
    expect(timeAgo(t - 600, now), '10 min ago');
    expect(timeAgo(t - 3 * 3600, now), '3 h ago');
    expect(timeAgo(t - 86400 - 60, now), 'yesterday');
    expect(timeAgo(t - 4 * 86400, now), '4 days ago');
  });

  test('new releases on Home and in the bell', () {
    final home = Home.fromJson({
      'sections': [],
      'releases': [
        {'source': 'deezer', 'id': 4, 'name': 'Brand New', 'artists': ['The Band'], 'type': 'single', 'release_date': '2026-10-07'},
      ],
      'unread_notifications': 2,
    });
    expect(home.releases.single.name, 'Brand New');
    expect(home.releases.single.typeLabel, 'Single');
    expect(home.releases.single.releaseDate, '2026-10-07');
    expect(home.unreadNotifications, 2);

    final n = AppNotification({
      'id': 9, 'kind': 'release', 'title': 'New single from The Band', 'body': 'Brand New', 'created': 1.0, 'read': false,
      'album': {'source': 'deezer', 'id': 4, 'name': 'Brand New', 'artists': ['The Band'], 'type': 'single'},
    });
    expect(n.album!.id, '4');
    expect(n.read, isFalse);
  });

  test('the artist page says whether you follow them', () {
    final page = ArtistPage.fromJson({'name': 'The Band', 'tracks': [], 'deezer_id': 7, 'following': true});
    expect(page.deezerId, 7);
    expect(page.following, isTrue);
    expect(ArtistPage.fromJson({'name': 'Unknown', 'tracks': []}).deezerId, isNull);
  });
}
