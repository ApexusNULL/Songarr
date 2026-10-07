# Songarr app

The Songarr music player, built with Flutter. It talks only to a Songarr server's app API
([../docs/API.md](../docs/API.md)). For setup (server, phone access, building and installing),
follow the main [README](../README.md#part-3-build-and-install-the-android-app).

Android is the supported platform. The code avoids platform-specific APIs where it can, so
Windows and Linux builds are possible later (see [Not yet](#not-yet)).

<p align="center"><img src="../docs/images/hero.jpg" alt="The Songarr app" width="760"></p>

## Quick start

```powershell
flutter pub get
flutter analyze
flutter test
flutter build apk --release --split-per-abi
```

The APKs land in `build\app\outputs\flutter-apk\`. Most phones need
`app-arm64-v8a-release.apk`.

### Optional files (never committed)

| File | What it does | Without it |
| --- | --- | --- |
| `android/key.properties` + your `.jks` | Signs release builds with your own key (copy `android/key.properties.example`) | Release builds use the debug key |
| `android/app/google-services.json` | Firebase: Jam invites arrive as notifications even when the app is closed | Invites appear while the app is open |

## Features

- **Sign in** by QR code, typed pairing code, or name and password. The device token is kept
  in Android's Keystore.
- **Home**: made-for-you songs, artists and podcasts (with 30-second previews), your playlists,
  and recently played, top and added songs.
- **Search** your library and Deezer's catalogue; **popular by genre** charts;
  request songs for the server to download next, or **whole albums** (soundtracks, film scores…).
- **Library**: Liked Songs, Downloads, requested songs, podcasts and playlists. Rename, reorder,
  add, remove and delete; arrange playlists by dragging (also on Home).
- **Artist pages** with bios and history; **synced lyrics**.
- **Podcasts**: follow, resume across devices, and downloads that expire.
- **Player**: background playback with notification, lock-screen, Bluetooth and headphone
  controls (press once to play or pause, twice for next, three times for previous); queue,
  shuffle, repeat and podcast speed. Tap the artist on Now Playing to open their page.
- **Backup servers**: the app knows every server's address and switches to whichever answers
  when its server doesn't, carrying on from the same song and spot.
- **Jams**: shared, synced listening with other profiles on the same server.
- **Recent searches**, and the last song comes back (paused where you left it) when the app
  reopens after being closed, force-closed or updated.
- **Updates** download from your own server and install themselves.
- **Look**: a drifting nebula background with a gyroscope depth effect, glass surfaces, an
  aurora gradient and sparkle effects (`lib/ui/fx/`). Respects Android's "remove animations".

## Songs on the device

Files are stored exactly as the server sends them (the original AAC or Opus, byte for byte)
and decoded only during playback.

| | Downloads | Listening cache |
| --- | --- | --- |
| How songs get there | The download button on a playlist, album or song | Automatically, while a song streams, and the next 3 songs of the queue ahead of time |
| Removed | Only by you | When the cache passes its size limit (1–20 GB, default 2 GB) |
| Plays offline | Yes | Yes |

When the cache is full, the songs you listen to least go first. Every listen adds to a song's
score (a full play counts 1, part of a play the fraction heard, a skip under 30 seconds
nothing), and the score halves every 30 days. See `lib/state/listen_scores.dart`. The song
playing and the songs fetched ahead are never removed.

**Songs ahead.** With every song change the app fetches the next 3 songs of the queue (in shuffle
order when shuffling) into the listening cache. Music then keeps playing for a while when the server
can't be reached: a backup server taking over (about a minute), a tunnel hiccup, or a dead spot. If
no server answers when the phone needs the next song, it keeps trying every 10 seconds for up to 3
minutes and carries on by itself.

## Development notes

- **Emulator**: the emulator reaches your PC as `10.0.2.2`, so pair with
  `http://10.0.2.2:8486`. Plain HTTP is allowed only for that address and the on-device
  streaming proxy (`android/app/src/main/res/xml/network_security_config.xml`).
- **Testing the depth effect on an emulator**: `adb emu sensor set gyroscope 0:1.5:0` turns
  the "phone"; `0:0:0` stops it.
- **Contract test**: see the main README's [Development](../README.md#development) section.

## Code map

```
lib/
  main.dart                    start-up, background audio, Firebase, sign-in gate
  api/client.dart              app API client, QR pairing parser
  api/models.dart              songs, playlists, podcasts, Jams, lyrics, bios…
  state/session.dart           server address + device token (secure storage), the other servers' addresses
  state/audio_handler.dart     the media session: notification, lock screen, headphone buttons
  state/player.dart            queue, playback sources, listen reporting, Jam mode, switching servers
  state/jam.dart               following a Jam's shared timeline, clock sync, drift correction
  state/push.dart              Firebase notifications for Jam invites (optional)
  state/offline.dart           downloads, listening cache, podcast mirroring, songs fetched ahead
  state/listen_scores.dart     decayed listen counts for cache pruning
  state/updates.dart           self-updates from the server
  state/search_history.dart    recent searches, per profile
  state/data.dart              data providers for each screen
  ui/                          screens: home, search, library, player, lyrics, podcasts, Jams, settings, sign-in
  ui/catalog_album_screen.dart  a whole album from the catalogue: track list and "Add album"
  ui/fx/                       nebula background, sparkles, depth effect (tilt.dart), motion helpers
test/                          unit tests + API contract test
```

## Not yet

- **Desktop builds**: Windows needs Developer Mode (for Flutter's plugin links) and Visual
  Studio's C++ tools, plus a desktop audio backend. Linux builds run on Linux or WSL.
- iOS, Android Auto and Chromecast, crossfade and an equaliser.
