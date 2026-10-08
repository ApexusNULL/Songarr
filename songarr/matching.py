"""Score how well a YouTube upload matches a song.

Signals, strongest first:
  duration   official audio is within a second or two of Spotify's length
  title      token overlap after stripping 'feat.' and remaster notes
  artist     a Spotify artist appears in the uploader/artists/title
  official   YouTube Music "song" results and "Artist - Topic" channels are label uploads
Version words (live, remix, sped up, 1 hour...) are penalised when they appear on one
side only, so "Song (Live)" never replaces "Song" and vice versa. So are cover acts that carry the
artist's name ("Queen at The Opera Original Cast", "The Beatles Tribute Band", "Vitamin String
Quartet") unless the song's own artist is one.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher


@dataclass
class TrackInfo:
    title: str
    artists: list[str]
    album: str | None
    duration: float | None  # seconds
    isrc: str | None = None

    @property
    def query(self) -> str:
        return f"{self.artists[0] if self.artists else ''} {strip_title(self.title)}".strip()


@dataclass
class Candidate:
    id: str
    title: str
    duration: float | None = None
    channel: str | None = None
    artists: list[str] = field(default_factory=list)
    track: str | None = None  # YouTube's own song-title metadata
    album: str | None = None
    source: str = "yt"  # isrc | ytm | yt
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


# Words that mark a different recording or a non-song upload.
VERSION_WORDS = [
    "live", "remix", "acoustic", "instrumental", "karaoke", "cover", "sped up", "speed up", "slowed",
    "reverb", "nightcore", "8d", "demo", "extended", "mashup", "bass boosted", "piano version",
    "orchestral", "a cappella", "acapella", "unplugged", "radio edit", "clean", "reprise",
]
JUNK_WORDS = ["1 hour", "10 hours", "hour loop", "reaction", "tutorial", "lesson", "how to play", "drum cover", "guitar cover"]
SOFT_WORDS = ["official video", "music video", "lyric video", "lyrics", "visualizer"]
# In an uploader's or credited artist's name, words that mark a cover act rather than the artist.
COVER_ACT_WORDS = [
    "tribute", "cast", "orchestra", "symphony", "philharmonic", "string quartet", "quartet", "ensemble",
    "karaoke", "players", "covers", "cover band", "in the style of", "made famous", "originally performed",
    "lullaby", "lullabies", "rockabye", "8 bit", "piano tribute", "revival", "experience", "the sound of",
]

# "(feat. X)", "[with X]" anywhere, or a bare "feat. X" tail; a bare "with" is left alone ("Dance with Me").
_FEAT = re.compile(r"\s*(?:[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+[^\)\]]*[\)\]]|\s(?:feat\.?|ft\.|featuring)\s+.*$)", re.I)
_REMASTER = re.compile(
    r"\s*(?:-\s*|\(|\[)\s*(?:\d{4}\s+)?(?:remaster(?:ed)?|digital(?:ly)? remaster(?:ed)?|mono|stereo|single version|"
    r"album version|original mix|bonus track|deluxe(?: edition)?)[^\)\]]*[\)\]]?",
    re.I,
)


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_title(title: str) -> str:
    """'Song (feat. X) - 2011 Remaster' -> 'Song'."""
    return _REMASTER.sub("", _FEAT.sub("", title)).strip(" -")


def _has(word: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def title_similarity(a: str, b: str) -> float:
    na, nb = norm(strip_title(a)), norm(strip_title(b))
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    # Containment: YouTube titles often carry the artist ("Artist - Song"), which must not dilute the score.
    contained = len(ta & tb) / len(ta)
    return max(SequenceMatcher(None, na, nb).ratio(), contained * (0.95 if contained == 1 else 0.85))


def score(track: TrackInfo, c: Candidate) -> Candidate:
    reasons: list[str] = []
    # duration
    d = None
    if c.duration is None or track.duration is None:
        dur = 0.4
        reasons.append("no duration")
    else:
        d = abs(c.duration - track.duration)
        if d > 30:
            c.score, c.reasons = 0.0, [f"length off by {d:.0f}s"]
            return c
        dur = 1.0 if d <= 2 else max(0.0, 1 - (d - 2) / 13)
        reasons.append(f"length ±{d:.0f}s")

    yt_title = c.track or c.title
    ttl = title_similarity(track.title, yt_title)
    if c.track and c.title and c.track != c.title:
        ttl = max(ttl, title_similarity(track.title, c.title))
    reasons.append(f"title {ttl:.2f}")

    hay = " | ".join(norm(x) for x in [*c.artists, (c.channel or "").removesuffix(" - Topic"), c.title])
    compact_hay = hay.replace(" ", "")
    # "Porno Graffitti" vs "PornoGraffitti", "Gesu No Kiwami Otome" vs "gesunokiwami otome"
    artist = 1.0 if any(
        norm(a) and (_has(norm(a), hay) or (len(norm(a)) >= 4 and norm(a).replace(" ", "") in compact_hay))
        for a in track.artists
    ) else 0.0
    reasons.append("artist ✓" if artist else "artist ✗")
    # Found by its ISRC (the recording's industry ID) and the same length to the second: that's
    # the same recording even when Spotify and YouTube spell the title or artist in different
    # scripts ("Joendanyusho" / "助演男優賞", "花冷え。" / "HANABIE.").
    isrc_exact = c.source == "isrc" and d is not None and d <= 1.5
    if isrc_exact:
        reasons.append("ISRC + exact length")

    # The artist only as part of a cover act's name ("Queen at The Opera Original Cast - Topic"): not the artist.
    credited = norm(" | ".join([*c.artists, (c.channel or "").removesuffix(" - Topic")]))
    sp_artists = norm(" ".join(track.artists))
    cover_act = next((w for w in COVER_ACT_WORDS if _has(w, credited) and not _has(w, sp_artists)), None)
    if cover_act and artist and not any(norm(a) and norm(a) in (norm(x) for x in c.artists) for a in track.artists):
        artist = 0.0
        reasons[-1] = f"artist ✗ (a cover act: {cover_act})"

    official = 1.0 if (c.channel or "").endswith(" - Topic") or (c.source in ("ytm", "isrc") and c.artists) else 0.0
    if official:
        reasons.append("official audio")

    s = 0.35 * dur + 0.30 * ttl + 0.20 * artist + 0.15 * official
    if c.album and track.album and norm(strip_title(c.album)) == norm(strip_title(track.album)):
        s += 0.05
        reasons.append("album ✓")

    sp_t, sp_a = norm(track.title), norm(track.album)
    yt_t, yt_a = norm(f"{c.title} {c.track or ''}"), norm(c.album)
    for w in VERSION_WORDS:
        # A version word on one side only means a different recording. Album names count as
        # context (a live album's tracks are often titled plainly) but never trigger on their own.
        if (_has(w, yt_t) and not (_has(w, sp_t) or _has(w, sp_a))) or (
            _has(w, sp_t) and not (_has(w, yt_t) or _has(w, yt_a))
        ):
            s -= 0.30
            reasons.append(f"version: {w}")
    for w in JUNK_WORDS:
        if _has(w, yt_t) and not _has(w, sp_t):
            s -= 0.50
            reasons.append(f"junk: {w}")
    if not official:
        for w in SOFT_WORDS:
            if _has(w, yt_t):
                s -= 0.03
    # Necessary conditions for an automatic download: the title really matches and a Spotify
    # artist is credited. Without them a same-length song by the same artist ("Square Dance"
    # for "Lose Yourself") or a cover upload could clear the bar on length alone.
    # With ISRC + exact length, one of title/artist matching is enough.
    if ttl < 0.6 and not (isrc_exact and artist):
        s = min(s, 0.45 + 0.25 * ttl)
        reasons.append("title mismatch")
    if not artist and not (isrc_exact and ttl >= 0.6):
        s = min(s, 0.6)
    c.score = round(max(0.0, min(1.0, s)), 3)
    c.reasons = reasons
    return c
