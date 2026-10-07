import 'dart:async';

import 'package:audio_service/audio_service.dart';
import 'package:audio_session/audio_session.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:just_audio/just_audio.dart';

/// The bridge between the player and the phone: the media session that drives the
/// notification, lock screen, Bluetooth/car controls and headphone buttons.
///
/// It owns the one [AudioPlayer] and reports its state to the system as it changes, so
/// play/pause from anywhere always matches what's really happening. Queue logic (what's
/// next, restarting a song, skipping songs that aren't downloaded) stays in
/// PlayerController, which plugs in through [onSkipToNext] and [onSkipToPrevious].
class SongarrAudioHandler extends BaseAudioHandler with SeekHandler {
  SongarrAudioHandler() {
    player.playbackEventStream.listen((_) => _broadcast(), onError: (Object _, StackTrace _) => _broadcast());
    player.playingStream.listen((_) => _broadcast());
    player.speedStream.listen((_) => _broadcast());
    player.currentIndexStream.listen((_) => _publishItem());
    player.durationStream.listen((_) => _publishItem());
    _configureSession();
  }

  final player = AudioPlayer();

  /// Set by PlayerController: its own idea of next/previous (e.g. restart if past 3 s), and
  /// play/pause/seek that go to a Jam when one is on.
  Future<void> Function()? onSkipToNext;
  Future<void> Function()? onSkipToPrevious;
  Future<void> Function()? onPlay;
  Future<void> Function()? onPause;
  Future<void> Function(Duration position)? onSeek;

  List<MediaItem> _items = const [];

  Future<void> _configureSession() async {
    try {
      final session = await AudioSession.instance;
      // Music: take audio focus, pause for calls, pause when headphones are unplugged or
      // Bluetooth disconnects (just_audio does the pausing once the session is set up).
      await session.configure(const AudioSessionConfiguration.music());
    } catch (_) {
      // Desktop platforms without an audio session: nothing to configure.
    }
  }

  /// What's queued, as the system should show it.
  void setItems(List<MediaItem> items) {
    _items = items;
    queue.add(items);
    _publishItem();
  }

  void _publishItem() {
    final i = player.currentIndex;
    if (i == null || i >= _items.length) return;
    var item = _items[i];
    final d = player.duration;
    if (d != null && d > Duration.zero && item.duration != d) item = item.copyWith(duration: d);
    if (mediaItem.value != item) mediaItem.add(item);
  }

  bool get _episode => mediaItem.value?.extras?['episode'] == true;

  void _broadcast() {
    final playing = player.playing;
    final episode = _episode;
    final controls = [
      episode ? MediaControl.rewind : MediaControl.skipToPrevious,
      playing ? MediaControl.pause : MediaControl.play,
      episode ? MediaControl.fastForward : MediaControl.skipToNext,
    ];
    playbackState.add(playbackState.value.copyWith(
      controls: controls,
      systemActions: const {
        MediaAction.seek,
        MediaAction.seekForward,
        MediaAction.seekBackward,
        MediaAction.skipToNext,
        MediaAction.skipToPrevious,
        MediaAction.skipToQueueItem,
        MediaAction.playPause,
        MediaAction.stop,
      },
      androidCompactActionIndices: const [0, 1, 2],
      processingState: switch (player.processingState) {
        ProcessingState.idle => AudioProcessingState.idle,
        ProcessingState.loading => AudioProcessingState.loading,
        ProcessingState.buffering => AudioProcessingState.buffering,
        ProcessingState.ready => AudioProcessingState.ready,
        ProcessingState.completed => AudioProcessingState.completed,
      },
      playing: playing,
      updatePosition: player.position,
      bufferedPosition: player.bufferedPosition,
      speed: player.speed,
      queueIndex: player.currentIndex,
    ));
  }

  // -- what the notification, lock screen, car and headphones ask for -------------------------

  @override
  Future<void> play() => onPlay?.call() ?? player.play();

  @override
  Future<void> pause() => onPause?.call() ?? player.pause();

  @override
  Future<void> seek(Duration position) => onSeek?.call(position) ?? player.seek(position);

  @override
  Future<void> skipToNext() => onSkipToNext?.call() ?? player.seekToNext();

  @override
  Future<void> skipToPrevious() => onSkipToPrevious?.call() ?? player.seekToPrevious();

  @override
  Future<void> skipToQueueItem(int index) => player.seek(Duration.zero, index: index);

  @override
  Future<void> setSpeed(double speed) => player.setSpeed(speed);

  @override
  Future<void> stop() async {
    await player.stop();
    await super.stop(); // tells the system playback is over: the notification goes away
  }

  @override
  Future<void> onTaskRemoved() async {
    // Swiped away from recents while paused: let the service stop. While playing, keep going.
    if (!player.playing) await stop();
  }

  // Headphone button: once = play/pause, twice = next song, three times = previous.
  // (Earbuds that send their own "next"/"previous" arrive as those buttons directly.)
  Timer? _clickTimer;
  int _clicks = 0;

  @override
  Future<void> click([MediaButton button = MediaButton.media]) async {
    if (button != MediaButton.media) return super.click(button);
    _clicks++;
    _clickTimer?.cancel();
    if (_clicks >= 3) {
      _clicks = 0;
      await skipToPrevious();
      return;
    }
    _clickTimer = Timer(const Duration(milliseconds: 380), () {
      final n = _clicks;
      _clicks = 0;
      if (n == 2) {
        skipToNext();
      } else if (player.playing) {
        pause();
      } else {
        play();
      }
    });
  }
}

/// Set in main() once the media service is running.
final audioHandlerProvider = Provider<SongarrAudioHandler>((ref) => throw StateError('audio handler not initialised'));
