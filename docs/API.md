# Songarr app API (v1)

The contract between the Songarr server and the Songarr player apps (Android, Windows,
Linux, macOS, iOS). JSON over HTTPS. Base URL: the **app address** set in Songarr's Settings,
for example `https://music.example.com`; locally `http://127.0.0.1:8486`.

## Rules

- Every endpoint except pairing needs `Authorization: Bearer <device token>`.
  Without it: `401 {"error": ...}`.
- Anything outside `/api/v1/` answers `404` with an empty body, except the app's download page
  (`/download`, its pictures and `/download/<name>.apk?abi=`), which the admin can switch off.
- Errors are `{"error": "message for the user"}` with a 4xx/5xx status.
- `503 {"error", "standby": true, "active": "https://...", "servers": [...]}` from every endpoint:
  this server is a backup standing by (see [Servers](#servers)). Switch to `active`, or to whichever
  of `servers` answers.
- IDs are opaque strings. Song IDs are Spotify track IDs (songs brought over with Exportify), or `dz<number>` for songs added
  from Deezer charts. App-made playlists start with `up`.

## Signing in (pairing)

An admin opens **People → Pair a phone or computer** in Songarr. It shows a QR code containing:

```
songarr://pair?server=<url-encoded app address>&code=<10 characters>
```

The code is single-use and expires after 10 minutes. The app can also accept it typed, as
`ABCDE-FGHJK`; dashes and case don't matter.

`POST /api/v1/auth/pair`  `{"code": "ABCDE-FGHJK", "device": "Pixel 9"}`
→ `{"token": "...", "user": {"id": 1, "name": "Alex"}}`

Store the token securely (Android Keystore / OS keychain). Wrong or expired codes get 401.
After 10 failures in 10 minutes an address is locked out.

**With a password.** A profile can also have a sign-in name and password, set on the PC (People)
or from a signed-in app (`POST /account/password` below).

`POST /api/v1/auth/login`  `{"login": "Sam", "password": "...", "device": "Pixel 9"}`
→ the same as pairing. Names ignore case. A wrong name or password gets 401; after 5 wrong
passwords for one name in 10 minutes (or 10 from one address) it's 429 until that passes.
Passwords are stored as scrypt hashes.

`POST /api/v1/auth/logout` revokes the current token.

## Track object

```json
{
  "id": "4uLU6hMCjMI75M1A2tKUQC", "title": "…", "artists": ["…"], "album": "…", "album_id": "…",
  "album_artists": ["…"], "release_date": "2014-05-01", "track_number": 1, "disc_number": 1,
  "duration_ms": 125000, "explicit": false, "cover_url": "https://…640", "thumb_url": "https://…64",
  "isrc": "…", "status": "downloaded", "playable": true, "format": "m4a", "bitrate": 129.6,
  "size": 2026957, "liked": true
}
```

`status`: `wanted | searching | downloading | downloaded | review | failed | ignored`.
Only `playable` songs can be streamed. Cover URLs are public CDN images, so load them directly.

## Endpoints

| Method & path | Returns |
| --- | --- |
| `GET /me` | `{user, spotify_accounts: [], server: {name, version, icon, background, see_through, servers}}`. `name` and `icon` are what the server's owner chose in Settings → Name and icon (`icon` changes with the picture, null for Songarr's own); `see_through` is true for a logo on a see-through background; `servers` is every server's app address, the active one first (see [Servers](#servers)). `spotify_accounts` is always empty (Songarr no longer reads Spotify; apps up to 1.8 expect the field) |
| `GET /branding/icon?size=` | the chosen icon as a PNG at least `size` pixels square (up to 512); `404` when none is chosen |
| `GET /home` | `{sections: [{id, title, tracks}], playlists, liked_count, for_you, podcasts}`. Section ids: `recently_played`, `top`, `recently_added`, `requested`. `for_you` (null while the server prepares it, usually seconds; ask again) is `{songs: [rec song], artists: [artist], top_artists: [artist], podcasts: [podcast + reason], computed}`. `podcasts` is `{following, continue, new_episodes}` as in `GET /podcasts`. `releases` are the new releases of the artists you follow (as in `GET /releases`), `unread_notifications` the bell's count |
| `GET /search?q=&limit=` | Searches the shared library: `{tracks, albums: [{id, name, artists, cover_url, year, count}], artists: [{name, cover_url, count}], top_artist}`. `top_artist` (`{name, image_url, thumb_url, fans, songs_on_server}` or null) is set when the search names an artist, even one with no songs here yet |
| `GET /tracks/{id}` | track |
| `GET /tracks/{id}/versions?search=` | `{track, current, current_url, expected_s, versions: [{youtube_id, title, channel, duration_s, score, official, current, url}]}`: the YouTube uploads found for the song, best match first, and the one it has (`current`). `search=1` searches YouTube again first (502 when YouTube isn't answering, 503 while it has asked the server to slow down) |
| `POST /tracks/{id}/replace` `{youtube}` | the track (status `wanted`): another version (a link or video id, from `versions` or anywhere) is downloaded next, in place of the one it has, for everyone. If it won't download, the song keeps its old file. 409 while the song is downloading |
| `GET /albums/{id}` | `{id, name, artists, cover_url, release_date, tracks}` |
| `GET /artist/about?name=` | `{about, pending}`. `about` is `{name, title, description, summary, history: [{heading, text}], facts: {type, formed/born, ended/died, origin, genres, labels, members}, image_url, url, source, license}` or null. `pending: true` while the server is still looking a new artist up: ask again in a few seconds |
| `GET /lyrics/{track_id}` | `{synced: [{t: ms, text}] or null, plain, instrumental, source}` from LRCLIB; `404` when there are none |
| `GET /artist?name=` | `{name, tracks (on the server, most played first), albums, about: artist or null, popular: [chart entry with in_library/status], related: [artist]}` (`about`/`popular`/`related` come from Deezer and are empty when it's unreachable). `deezer_id` (null when Deezer doesn't know them) and `following` are for the Follow button |
| `GET /library` | `{liked: {count}, playlists: [{id, name, kind: "app", owner, image_url, count, editable, from_spotify}]}`, in your order (see below; by default most recently changed first). `from_spotify: true` for one brought over from Spotify (an ordinary playlist of yours all the same); `GET /playlists/{spotify id}` opens it |
| `POST /library/liked/move` `{from, to, track_id}` | `{ok}`. Moves a song within your Liked Songs (409 if the list changed meanwhile) |
| `PUT /library/order` `{playlist_ids: [...]}` | the library, with playlists in that order (your own arrangement; ids you don't have are ignored). Playlists not in it, like new ones, are listed first |
| `GET /library/liked?offset=&limit=` | `{total, offset, tracks}` (up to 500 per page), newest like first, or in your order once you've moved something (songs liked since then come first) |
| `GET /playlists/{id}?offset=&limit=` | `{id, name, kind, editable, owner, image_url, total, offset, tracks}` |
| `POST /playlists` `{name, description?}` | new app playlist |
| `PATCH /playlists/{id}` `{name?, description?}` | playlist |
| `DELETE /playlists/{id}` | `{ok}` |
| `POST /playlists/{id}/tracks` `{track_ids: [...]}` | `{added}`; appends; unknown IDs are skipped |
| `DELETE /playlists/{id}/tracks/{position}` | `{ok}`; later positions shift up |
| `POST /playlists/{id}/tracks/move` `{from, to, track_id}` | `{ok}`. Moves the song at `from` to `to`. `track_id` is the song you mean: if the playlist changed meanwhile, nothing moves (409) |
| `PUT /likes/{track_id}` | `{liked: true}` |
| `DELETE /likes/{track_id}` | `{liked: false, on_spotify: false}` (`on_spotify` is always false: apps up to 1.8 expect it) |
| `POST /plays` `{track_id, ms_played, completed}` | `{ok}`. Send when a song ends or is skipped; feeds Home |
| `GET /stream/{track_id}` | the audio file (see below) |
| `GET /app/update?abi=&build=` | `{available, update: {version, build, notes, published, size, sha256, abi} or null}`. `abi` is the phone's processor type (`arm64-v8a`, `armeabi-v7a`, `x86_64`); `build` the installed build number |
| `GET /app/update/apk?abi=` | the newest APK for that processor type (`Range` works). Verify `sha256` before installing |
| `GET /catalog/search?q=` | Songs anywhere, not just the library: `{source: "deezer", results: [{source, id, title, artists, album, duration_ms, cover_url, thumb_url, in_library, status}]}` |
| `POST /requests` `{source, id, like?}` | Pass a `catalog/search`, chart or recommended (`source: "deezer"`) result. Returns the track, now queued ahead of the backlog and of earlier requests (the newest request downloads first); poll `GET /tracks/{id}` until `playable`. `like: true` also adds it to your Liked Songs (the "Like" on a preview) |
| `GET /catalog/albums?q=` | Whole albums anywhere (Deezer): `{albums: [{source, id, name, artists, type: album/ep/single/compile, count, explicit, cover_url, thumb_url, on_server}]}`. Full albums before singles; `on_server` counts songs already downloaded (by album name and artist) |
| `GET /catalog/albums/deezer/{id}` | One album with its full track list: `{…album, release_date, year, label, genres, count, on_server, added, tracks: [catalog item + track_number, disc_number, in_library, status, track]}`. `track` is the server's copy when it has the song (same recording by ISRC, or same title and artist on an album of the same name); `added` is true when every song is in your library |
| `POST /requests/album` `{source: "deezer", id}` | Add a whole album: songs not on the server are queued ahead of the backlog, in album order; songs already there join your library. Returns the album as above |
| `GET /preview?source=&id=&isrc=&artist=&title=` | A 30-second MP3 preview of a song that isn't on the server (Deezer's, passed through; `Range` works). Found by Deezer id, else ISRC, else artist + title. `404` when there's none |
| `GET /requests` | `{requests: [track]}`, your requests, newest first |
| `GET /discover/genres` | `{genres: [{id, name, image}]}` (Deezer genres; 0 = all) |
| `GET /discover/genres/{id}?limit=` | `{genre_id, tracks: [chart entry with in_library/status]}`: popular songs in that genre |
| `GET /podcasts` | `{following: [podcast], continue: [episode] (partly heard), new_episodes: [episode] (last 3 weeks, unheard), recommended: [podcast + reason]}`. Following = the shows you follow |
| `GET /podcasts/search?q=` | `{results: [podcast]}` from the Apple Podcasts directory |
| `GET /podcasts/{id}?limit=` | podcast + `{description, website, on_spotify (always false), stale, downloads, episodes: [episode with description]}`; re-reads the feed if it's over 30 minutes old (`stale: true` when that failed) |
| `PUT` / `DELETE /podcasts/{id}/follow` | `{following}` (`DELETE` also returns `on_spotify: false`, for apps up to 1.8) |
| `PUT /podcasts/{id}/downloads` `{keep}` | `{downloads}`. Download the show's newest episodes (last 30 days, 2 at a time): `keep` = `off`, `played` (deleted once you finish each) or `3`/`7`/`14`/`30` days |
| `PUT /podcasts/episodes/{id}/download` `{keep}` | Keep one episode downloaded until `played` (default) or for that many days. Returns `{keep, expires, on_server, size}` |
| `DELETE /podcasts/episodes/{id}/download` | `{ok}`: you no longer keep it (the file goes when nobody does) |
| `GET /podcasts/downloads` | `{episodes: [episode], downloading}`: everything you keep. Apps mirror the ones with `on_server: true` and delete local copies that drop off this list |
| `POST /podcasts/episodes/{id}/progress` `{position_ms, duration_ms?, completed}` | `{ok}`. Send on pause, every 30 s or so, and when switching away; feeds resume and "until played" |
| `GET /podcasts/episodes/{id}/stream` | the episode's audio: the server's copy if downloaded, otherwise passed through from the podcast's host (`Range` works either way) |
| `GET /artists/following` | `{artists: [{deezer_id, name, image_url, followed_at, latest: {name, release_date, type} or null}]}`, by name |
| `POST /artists/follow` `{name, deezer_id?}` | Follow an artist (found by name when `deezer_id` is missing; 404 if the catalogue doesn't know them). Their new releases reach this profile |
| `POST /artists/unfollow` `{deezer_id}` | Stop following |
| `GET /releases?limit=` | `{releases: [album]}`: releases from the artists you follow from the last 60 days, newest first. Each album is as in `GET /catalog/albums` plus `release_date`; open it with `GET /catalog/albums/deezer/{id}` |
| `GET /notifications?limit=` | `{notifications: [{id, kind: "release", title, body, created, read, album}], unread}`, newest first |
| `POST /notifications/read` `{ids?}` | Mark these (or all) read; returns `{unread}` |
| `GET /account` | `{name, login, has_password}` |
| `POST /account/password` `{login, password, current?}` | Set or change this profile's sign-in name and password (8+ characters). Changing an existing password needs `current` (403 if wrong). Returns `{name, login, has_password}` |
| `GET /people` | `{people: [{id, name}]}`: the other profiles, to invite to a Jam |
| `POST /jams` `{items, index, position_ms, playing, invite: [user id]}` | Start a Jam (listening together) and invite people: returns the jam. `items` is the list to play, `[{track_id} or {episode_id}]` (songs not on the server are dropped, so send playable ones and an `index` into that list). You leave any other Jam |
| `GET /jams/current` | `{jam (or null), invites: [{id, from, members, item}], server_time}` |
| `GET /jams/{id}?v=&wait=` | the jam. With `v` (the version you have) and `wait` (seconds, up to 25) it waits until something changes: a long poll |
| `POST /jams/{id}/join` / `decline` / `leave` | the jam / `{ok}` / `{ok}`. When the host leaves, someone else hosts; it ends when everyone has left (or stopped checking in for 2 minutes) |
| `POST /jams/{id}/invite` `{invite: [user id]}` | the jam |
| `POST /jams/{id}/control` `{action, ...}` | the jam. `play`/`pause` `{position_ms}`, `seek` `{position_ms}`, `next`/`prev`/`jump` `{index?, expected_index}` (ignored if the Jam already moved on, so two phones finishing together skip once), `add` `{items, next?}` (to the end, or `next: true` after what's on), `replace` `{items, index}` (someone picked from another list: play that, from there) |
| `POST /devices/push` `{token}` | Store this device's Firebase token so Jam invites arrive as notifications; `{ok, push}` (`push: false` when the server has no Firebase key) |

Recommendation and podcast objects:

- **rec song**: `{source: "deezer"/"library", id, title, artists, album, duration_ms, explicit, cover_url, thumb_url, reason, in_library, status, track}`. `track` is set when the server has the song (play it once `playable`); otherwise preview it and request it with `{source, id}`.
- **artist**: `{name, image_url, thumb_url, deezer_id, fans?, reason?, songs_on_server?}`.
- **podcast**: `{id ("ap…"), title, author, artwork_url, genre, following}`.
- **episode**: `{id ("ep…"), podcast_id, podcast_title, title, published (unix seconds), duration_ms, image_url, progress_ms, completed, keep, expires, on_server, size}`.

**Jam** objects: `{id, host: {id, name}, members: [{id, name}], invited, queue: [track or episode], index,
playing, position_ms, ref, server_time, version, ended}`. The timeline is "at server time `ref`
(ms), `queue[index]` is at `position_ms`" (and moving on if `playing`), so every phone can work
out where the song is now; starts are scheduled a moment ahead (`ref` in the future) so phones
begin together. Apps estimate the server clock from `server_time` and their request round trips.
When the queue runs out the server plays the list it came from again (5+ songs), or adds songs
the people in the Jam like; a Jam of podcast episodes just stops at the end.

Podcast downloads expire, unlike songs: an episode stays on the server while any profile keeps it, and its file is deleted once nobody does. They're stored under the `podcast_root` setting (default: a `Podcasts` folder next to the music library).

## Servers

A family can run more than one server (a main one and backups, sharing their music on a NAS). One is
active at a time; the others answer every request with the `503` above. `GET /me` lists them all in
`server.servers` (each server's app address, the active one first), and the same device token works
on every one of them.

When a request can't reach the server (no connection, a timeout) or gets `502`, `503`, `504` or
Cloudflare's `530`, try the others: `GET /me` on each, and use the first that answers `200`. A backup
takes over within about a minute of the active server stopping, so keep a few songs ahead on the phone
(the official app keeps the next 3) and keep trying every few seconds meanwhile.

## Streaming and offline cache

`GET /stream/{id}` (and `HEAD`) serves the original file, byte-for-byte, with:

- `Content-Type`: `audio/mp4` (m4a/AAC), `audio/ogg` (Opus) or `audio/mpeg` (MP3).
- `Accept-Ranges: bytes`: `Range: bytes=a-b`, `bytes=a-` and `bytes=-n` give `206` with
  `Content-Range`, so seeking works. An unsatisfiable range gives `416`.
- `ETag`: send `If-None-Match` to get `304` when your cached copy is current.
- `404` if the song isn't downloaded yet. If its file went missing on the server, Songarr
  starts downloading it again.

**Cache files exactly as received.** They're already compressed (AAC/Opus). Compressing them
again losslessly saves about 1%, and converting to FLAC makes them 5 to 8 times bigger with no
quality gain. The player decodes the original file during playback, so nothing is lost.
