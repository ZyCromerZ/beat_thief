"""Fetch song lyrics and store them in a sidecar .lrc file next to the audio file.

Lyrics are never written into the music file itself: they live in a separate
"<name>.lrc" file beside it, so the audio is left untouched. The lookup uses the
LRCLIB API (https://lrclib.net), which is free and needs no API key. It first
tries to match the exact track by artist + title + duration; if that fails it
falls back to the best search hit.

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


def fetch_lyrics(artist: str, title: str, duration: float = None):
    """Return (text, source) where text is plain or synced lyrics and source is a short label.

    Returns (None, reason) when no lyrics could be found. Never raises on network errors: it
    logs and returns a reason so the UI can report it without failing the whole batch.
    """
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not title:
        return None, "No title to search for."
    dur = int(duration) if duration else None

    # 1) Exact lookup by artist + title + duration.
    if artist and dur:
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

    # 2) Search and pick the best hit.
    params = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
    try:
        hits = _http_json(f"{_SEARCH_URL}?{params}")
    except Exception as e:
        log.debug("LRCLIB search (%s - %s) failed: %s", artist, title, e, exc_info=verbose())
        return None, "Could not reach the lyrics service."

    if not isinstance(hits, list) or not hits:
        return None, "No match found."
    best = _pick(hits)
    if not best or best.get("instrumental"):
        return None, "Track is marked instrumental."
    text = best.get("syncedLyrics") or best.get("plainLyrics")
    if not text:
        return None, "No lyrics for this track."
    return text, "search match"


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