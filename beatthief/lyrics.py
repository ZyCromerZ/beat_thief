"""Fetch song lyrics and store them in a sidecar .lrc file next to the audio file.

Lyrics are never written into the music file itself: they live in a separate
"<name>.lrc" file beside it, so the audio is left untouched. The lookup uses the
LRCLIB API (https://lrclib.net), which is free and needs no API key. It first
tries to match the exact track by artist + title + duration; if that fails it
falls back to progressively looser searches.

Nightcore (and other sped-up) uploads are common and awkward for a lyrics lookup:
the "artist" is often the upload channel (e.g. "NightcoreClarity"), the title
carries a "(Nightcore Remix)" marker, and the duration is the shortened, sped-up
one, which never matches the original track's length. So the title and artist are
cleaned of these markers, and when the artist is just a nightcore channel it is
dropped so the search runs on the title alone.

The .lrc format is a timestamped lyrics file. When LRCLIB returns synced lyrics
we keep them as-is; when only plain text is available we wrap every line with a
timestamp at 0 ms so the file still opens as a valid .lrc.
"""
import re
import urllib.parse
import urllib.request

from .log import log, verbose

# LRCLIB endpoints. No authentication is needed for these.
_LRC_BASE = "https://lrclib.net/api"
_SEARCH_URL = f"{_LRC_BASE}/search"
_GET_URL = f"{_LRC_BASE}/get"
_MAX_BYTES = 2_000_000
_TIMEOUT = 15
_UA = "BeatThief/1.0 (https://github.com/Kilo-Org/beat_thief)"


def _http_json(url: str):
    """GET a URL and return parsed JSON. Raises on any failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = resp.read(_MAX_BYTES)
    if not data:
        raise RuntimeError("Empty response from the lyrics service.")
    import json
    return json.loads(data.decode("utf-8", "replace"))


def _is_synced(text: str) -> bool:
    """A synced LRC line looks like '[mm:ss.xx]...'. If any line has one, the file is synced."""
    return bool(re.search(r"^\s*\[\d{1,3}:\d{2}([.,]\d{1,3})?\]", text, re.M))


def _as_lrc(text: str) -> str:
    """Return the lyrics as a valid .lrc string: keep synced lyrics, timestamp plain text at 0."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if _is_synced(text):
        return text + "\n"
    # Plain text: give every line a 00:00.00 timestamp so it's still a valid .lrc.
    lines = [ln for ln in text.split("\n")]
    return "".join(f"[00:00.00]{ln}\n" for ln in lines)


def _pick(hit: dict):
    """Pick the best synced result from a search hit, else any with lyrics."""
    if not hit:
        return None
    synced = [h for h in hit if h.get("syncedLyrics")]
    if synced:
        # Prefer an exact (non-instrumental) match; otherwise the first.
        for h in synced:
            if not h.get("instrumental"):
                return h
        return synced[0]
    plain = [h for h in hit if h.get("plainLyrics") and not h.get("instrumental")]
    return plain[0] if plain else (hit[0] if hit and hit[0].get("plainLyrics") else None)


# A sped-up upload ("Nightcore", "Sped Up", "Nightcore Remix", ...). Nightcore keeps the original
# lyrics word for word, just faster and higher, so stripping these markers lets the lookup find the
# original song the lyrics actually belong to.
_SPEED_MARKER = re.compile(r"night[\s-]?core|sped[\s-]?up|speed[\s-]?up", re.I)
_BRACKET = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]")


def _strip_speed_markers(text: str) -> str:
    """Drop '(Nightcore)', '[Sped Up]', trailing 'Nightcore', etc. from a title/artist."""
    def keep(m):
        return "" if _SPEED_MARKER.search(m.group(1)) else m.group(0)
    text = _BRACKET.sub(keep, text or "")
    text = _SPEED_MARKER.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip(" -–—|")


def _has_speed_marker(text: str) -> bool:
    """True when a genre, artist or title marks a sped-up upload (Nightcore, Sped Up, ...)."""
    return bool(_SPEED_MARKER.search(text or ""))


# "Song (feat. X)", "Song (ft. X)", "Song (featuring X)" - the featured artist is not part of the
# title the lyrics are filed under, so drop it to match the original track.
_FEAT = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\b[^\)\]]*[\)\]]", re.I)
# Artists in a collab are stored joined ("A, B, C" / "A & B" / "A x B"); split them so each can be
# searched. A bare "x" only counts as a separator with spaces around it, so "Malcolm X" is kept.
_ARTIST_SPLIT = re.compile(r"\s*[,&/;×]\s*|\s+x\s+|\s+(?:feat\.?|ft\.?|featuring)\s+", re.I)


def _strip_feat(text: str) -> str:
    """Drop a trailing '(feat. X)' / '(ft. X)' / '(featuring X)' from a title."""
    prev = None
    text = text or ""
    while prev != text:  # a title can carry more than one, e.g. "(feat. A) (feat. B)"
        prev = text
        text = _FEAT.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip(" -–—|")


def _split_artists(artist: str):
    """Split a joined artist field into individual names, preserving order and dropping duplicates."""
    parts = [p.strip() for p in _ARTIST_SPLIT.split(artist or "") if p and p.strip()]
    seen, out = set(), []
    for p in parts:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def _norm(text: str) -> str:
    """Lower-case, drop punctuation and collapse spaces, for comparing titles loosely."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def _search(params: dict):
    return _http_json(f"{_SEARCH_URL}?{urllib.parse.urlencode(params)}")


def _from_hits(hits, exact_title: str):
    """Pick the best hit. With exact_title set, only accept a near-exact title match so a looser
    search can't silently return a different song's lyrics; otherwise take the best synced result."""
    if not isinstance(hits, list) or not hits:
        return None
    if exact_title:
        want = _norm(exact_title)
        hits = [h for h in hits if _norm(h.get("trackName")) == want]
    return _pick(hits) if hits else None


def fetch_lyrics(artist: str, title: str, duration: float = None, genre: str = None, album: str = None):
    """Return (text, source) where text is plain or synced lyrics and source is a short label.

    Returns (None, reason) when no lyrics could be found. Never raises on network errors: it
    logs and returns a reason so the UI can report it without failing the whole batch.

    Nightcore / sped-up uploads are recognised by the genre, or by a marker in the artist or
    title, and handled accordingly: the speed markers are stripped from the title, an 'artist'
    that is really just a nightcore channel is dropped, the (shortened) duration is not trusted
    for the exact match, and a title-only fallback is allowed. That fallback only accepts a
    near-exact title, so it never returns a different song that merely shares a word.

    When the artist + title search comes up empty, the album is used as an extra clue: first
    title + album + artist, then title + album.
    """
    # A nightcore channel name (e.g. "NightcoreClarity") or a sped-up genre flags the track as a
    # nightcore even when the title itself carries no marker.
    is_nightcore = (_has_speed_marker(genre) or _has_speed_marker(artist)
                    or _has_speed_marker(title))
    if _has_speed_marker(artist):
        artist = ""  # the "artist" is the upload channel, not the performer
    artist = _strip_speed_markers((artist or "").strip())
    title = _strip_feat(_strip_speed_markers((title or "").strip()))
    album = _strip_speed_markers((album or "").strip())
    if not title:
        return None, "No title to search for."
    dur = int(duration) if duration else None
    # The full artist, plus each collaborator on its own (a joined "A, B, C" never matches a
    # single-artist record, but one of the names usually does).
    artists = [artist] if artist else []
    for name in _split_artists(artist):
        if name.lower() != (artist or "").lower():
            artists.append(name)

    # 1) Exact lookup by artist + title + duration (only when we trust the artist and the length).
    # A nightcore is sped up, so its duration is shorter than the original and can never match it.
    if artist and dur and not is_nightcore:
        params = urllib.parse.urlencode({
            "artist_name": artist, "track_name": title,
            "album_name": "", "duration": dur,
        })
        try:
            data = _http_json(f"{_GET_URL}?{params}")
            if isinstance(data, dict) and not data.get("instrumental"):
                text = data.get("syncedLyrics") or data.get("plainLyrics")
                if text:
                    return text, "exact match"
        except Exception as e:
            log.debug("LRCLIB get (%s - %s) failed: %s", artist, title, e, exc_info=verbose())

    # 2) Search. With a real artist, require it (a title-only hit would likely be a different
    # song). Without one, or for a nightcore (whose "artist" is often just a channel), also try
    # the title alone and accept only a near-exact title match, then a free-text search.
    # The album is an extra clue when the artist-based search fails: title + album + artist, then
    # title + album.
    attempts = []
    for name in artists:
        attempts.append(({"track_name": title, "artist_name": name}, None))
    if album and artists:
        attempts.append(({"track_name": title, "album_name": album, "artist_name": artists[0]}, None))
    if album:
        attempts.append(({"track_name": title, "album_name": album}, title))
    if not artist or is_nightcore:
        attempts.append(({"track_name": title}, title))
        attempts.append(({"q": title}, title))

    last_reason = "No match found."
    for params, exact in attempts:
        try:
            hits = _search(params)
        except Exception as e:
            log.debug("LRCLIB search (%s) failed: %s", params, e, exc_info=verbose())
            return None, "Could not reach the lyrics service."

        best = _from_hits(hits, exact)
        if not best or best.get("instrumental"):
            if best:
                last_reason = "Track is marked instrumental."
            continue
        text = best.get("syncedLyrics") or best.get("plainLyrics")
        if text:
            return text, "search match"
    return None, last_reason


def sidecar_path(path) -> "Path":
    """Return the .lrc file path that belongs to the given audio file path."""
    from pathlib import Path
    p = Path(path)
    return p.with_suffix(".lrc")


def save_lyrics(path, text: str) -> "Path":
    """Write the lyrics as a .lrc sidecar next to `path`. Returns the sidecar path."""
    lrc = sidecar_path(path)
    lrc.write_text(_as_lrc(text), encoding="utf-8")
    return lrc