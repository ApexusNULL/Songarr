package com.ryanheise.audioservice;

import android.content.Intent;
import android.support.v4.media.session.MediaSessionCompat;
import android.view.KeyEvent;

import java.lang.ref.WeakReference;
import java.lang.reflect.Field;

/**
 * Songarr's handling of the media keys that say which way to go: a "pause" (from Bluetooth
 * headphones, a car or a watch) only ever pauses, and a "play" only ever plays.
 *
 * audio_service passes both to the app as a plain button click, which the app takes as play/pause
 * (once: play/pause, twice: next, three times: previous), so a car sending "pause" while Songarr was
 * already paused started the music. The play/pause key and a wired headset's button still arrive as
 * clicks. (The notification's own play and pause buttons use audio_service's other key codes and
 * are untouched.)
 *
 * This sits in audio_service's package for its running service; the media session is found by its
 * type, as release builds rename private fields.
 */
public final class SongarrMediaKeys {
    private static WeakReference<AudioService> installedOn = new WeakReference<>(null);

    private SongarrMediaKeys() {}

    /** Takes over the keys on the running service's media session (again, if the service was made afresh). */
    public static boolean install() {
        final AudioService service = AudioService.instance;
        if (service == null) return false;
        if (installedOn.get() == service) return true;
        try {
            for (Field field : AudioService.class.getDeclaredFields()) {
                if (field.getType() != MediaSessionCompat.class) continue;
                field.setAccessible(true);
                final MediaSessionCompat session = (MediaSessionCompat) field.get(service);
                if (session == null) return false;
                session.setCallback(new Keys(service));
                installedOn = new WeakReference<>(service);
                return true;
            }
        } catch (Exception e) {
            // audio_service changed inside: its own handling stays
        }
        return false;
    }

    private static final class Keys extends AudioService.MediaSessionCallback {
        Keys(AudioService service) {
            service.super();
        }

        @Override
        public boolean onMediaButtonEvent(Intent mediaButtonEvent) {
            @SuppressWarnings("deprecation")
            final KeyEvent event = mediaButtonEvent.getParcelableExtra(Intent.EXTRA_KEY_EVENT);
            if (event != null) {
                switch (event.getKeyCode()) {
                case KeyEvent.KEYCODE_MEDIA_PLAY:
                    if (event.getAction() == KeyEvent.ACTION_DOWN) onPlay();
                    return true;
                case KeyEvent.KEYCODE_MEDIA_PAUSE:
                    if (event.getAction() == KeyEvent.ACTION_DOWN) onPause();
                    return true;
                default:
                    break;
                }
            }
            return super.onMediaButtonEvent(mediaButtonEvent);
        }
    }
}
