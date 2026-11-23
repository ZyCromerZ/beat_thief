"""Fetch song lyrics and store them in a sidecar .lrc file next to the audio file.

Lyrics are never written into the music file itself: they live in a separate
"<name>.lrc" file beside it, so the audio is left untouched. The lookup uses three
sources in order: the LRCLIB API (https://lrclib.net) first, then lrc.red
(https://lrc.red), and finally the video's own YouTube captions (read from the
source link stored in the file's comment tag) as a last resort for non-official
uploads the lyric databases don't have. Each database lookup first tries to match
the exact track by artist + title + duration; if that fails it falls back to
progressively looser searches.

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

from . import config
from .log import log, verbose

# LRCLIB endpoints. No authentication is needed for these.
_LRC_BASE = "https://lrclib.net/api"
_SEARCH_URL = f"{_LRC_BASE}/search"
_GET_URL = f"{_LRC_BASE}/get"
# lrc.red endpoints. Free to use; it asks that we identify ourselves via the User-Agent
# and credit it (a link to https://lrc.red) wherever its lyrics are used.
_LRCRED_BASE = "https://lrc.red"
_LRCRED_SEARCH_URL = f"{_LRCRED_BASE}/search.json"
_LRCRED_TRACK_URL = f"{_LRCRED_BASE}/s"
_MAX_BYTES = 2_000_000
_TIMEOUT = 15
_UA = "BeatThief/1.0 (https://github.com/Kilo-Org/beat_thief)"

# Lookups are done one at a time, this many seconds apart, so a big batch doesn't hammer the
# services into returning "Could not reach the lyrics service" (rate limiting / HTTP 429).
DEFAULT_LYRICS_DELAY = 1.0


def lyrics_delay() -> float:
    """Seconds to wait between two lyric lookups (config "lyrics_delay"; 0 disables the wait)."""
    try:
        return max(0.0, float(config.get_setting("lyrics_delay", DEFAULT_LYRICS_DELAY)))
    except (TypeError, ValueError):
        return DEFAULT_LYRICS_DELAY


def auto_lyrics_enabled() -> bool:
    """Whether to fetch lyrics automatically after each download (config "auto_lyrics")."""
    return bool(config.get_setting("auto_lyrics", False))


def _http_json(url: str):
    """GET a URL and return parsed JSON. Raises on any failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = resp.read(_MAX_BYTES)
    if not data:
        raise RuntimeError("Empty response from the lyrics service.")
    import json
    return json.loads(data.decode("utf-8", "replace"))


def _http_text(url: str) -> str:
    """GET a URL and return the body as text. Raises on any failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = resp.read(_MAX_BYTES)
    if not data:
        raise RuntimeError("Empty response from the lyrics service.")
    return data.decode("utf-8", "replace")


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


def _lrc_red_search(q: str):
    """Search lrc.red's catalog (GET /search.json) and return the list of hits."""
    data = _http_json(f"{_LRCRED_SEARCH_URL}?{urllib.parse.urlencode({'q': q})}")
    hits = data.get("hits") if isinstance(data, dict) else None
    return hits if isinstance(hits, list) else []


def _lrc_red_pick(hits, artist: str, title: str, duration: float):
    """Choose the hit that best matches the wanted title/artist/duration from lrc.red."""
    want_title = _norm(title)
    candidates = [h for h in hits if _norm(h.get("title")) == want_title]
    if not candidates:
        return None
    want_artist = _norm(artist)
    dur = int(duration) if duration else None

    def score(h):
        s = 0
        if want_artist and want_artist in (_norm(h.get("artist")) or ""):
            s += 2
        if dur and h.get("duration"):
            try:
                if abs(float(h["duration"]) - dur) <= 5:
                    s += 2
            except (TypeError, ValueError):
                pass
        return s

    candidates.sort(key=score, reverse=True)
    return candidates[0]


# A line of a lrc.red .lrc / .ttml: '[mm:ss.xx]' line stamps and '<mm:ss.xx>' word stamps.
_LRC_LINE_STAMP = re.compile(r"\[\d{1,3}:\d{2}([.,]\d{1,3})?\]")
_LRC_WORD_STAMP = re.compile(r"<\d{1,3}:\d{2}([.,]\d{1,3})?>")


def _strip_lrc_times(text: str) -> str:
    """Turn a synced/enhanced .lrc into plain text by removing its line and word timestamps."""
    text = _LRC_WORD_STAMP.sub("", text)
    text = _LRC_LINE_STAMP.sub("", text)
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()


# A line entry of a lrc.red .lyricsfile.yaml ('  - text: "..."'); the deeper word entries ('      - text:')
# and the metadata keys ('  title:') are deliberately not matched, so only the full line texts remain.
_LYICSFILE_LINE = re.compile(r'^  - text:\s*(?P<q>["\']?)(?P<text>.*?)(?P=q)\s*$')


def _lyricsfile_to_text(yaml_text: str) -> str:
    """Extract the plain line texts from a lrc.red .lyricsfile.yaml (no PyYAML needed)."""
    out = []
    for raw in (yaml_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = _LYICSFILE_LINE.match(raw)
        if not m:
            continue
        text = m.group("text")
        if m.group("q") == '"':  # unescape the common YAML double-quote escapes
            text = text.replace('\\"', '"').replace("\\n", "\n").replace("\\/", "/").replace("\\\\", "\\")
        elif m.group("q") == "'":
            text = text.replace("''", "'")
        out.append(text)
    return "\n".join(out).strip()


def _lrc_red_lookup(artist: str, title: str, duration: float = None):
    """Fetch lyrics from lrc.red for the given track. Returns the text or None.

    lrc.red keys its catalog by ISRC, so this searches the catalog for a matching track and then
    downloads that track's lyrics. It returns synced lyrics when the track is timed; otherwise
    (typically non-official / untimed songs) it falls back to plain text, read from a non-synced
    .lrc or, failing that, from the track's .lyricsfile.yaml.
    """
    if not title:
        return None
    # The "q" field is "title or artist", so search on the title, then on "artist title" as a
    # fallback. Only accept a near-exact title match so a looser search can't return a different
    # song's lyrics.
    queries = [title]
    if artist:
        queries.append(f"{artist} {title}")
    for q in queries:
        try:
            hits = _lrc_red_search(q)
        except Exception as e:
            log.debug("lrc.red search (%s) failed: %s", q, e, exc_info=verbose())
            return None
        best = _lrc_red_pick(hits, artist, title, duration)
        if not best or not best.get("isrc"):
            continue
        isrc = best["isrc"]
        # 1) Synced .lrc (official / timed tracks). A non-synced .lrc is kept as plain text.
        try:
            text = _http_text(f"{_LRCRED_TRACK_URL}/{isrc}.lrc")
        except Exception as e:
            log.debug("lrc.red lrc fetch (%s) failed: %s", isrc, e, exc_info=verbose())
            text = ""
        if text:
            if _is_synced(text):
                return text
            plain = _strip_lrc_times(text)
            if plain:
                return plain
        # 2) No usable .lrc: read the line texts straight out of the .lyricsfile.yaml as plain text.
        try:
            yaml_text = _http_text(f"{_LRCRED_TRACK_URL}/{isrc}.lyricsfile.yaml")
        except Exception as e:
            log.debug("lrc.red lyricsfile fetch (%s) failed: %s", isrc, e, exc_info=verbose())
            continue
        plain = _lyricsfile_to_text(yaml_text)
        if plain:
            return plain
    return None


# --- YouTube captions -----------------------------------------------------------------------------
# Each downloaded file keeps its source link in the comment tag. yt-dlp can pull that video's
# captions (manual or auto-generated), which for non-official uploads (nightcore, fan covers, label
# rips) are the only lyrics that actually match the audio. Captions are converted to .lrc below.

# A cue timestamp: 'HH:MM:SS.mmm' or 'MM:SS.mmm' (VTT and SRT both appear in the wild).
_CUE_TS = re.compile(r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,3}:\d{2}[.,]\d{1,3})")
# An inline word-level timing tag in auto captions, e.g. '<00:00:19.039>'.
_WORD_TS = re.compile(r"<\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}>")
# Any remaining inline tag ('<c>', '<i>', ...), stripped from the displayed text.
_ANY_TAG = re.compile(r"<[^>]*>")
# A line that is only a music/sound marker, e.g. '[Music]', '(♪)', '***'.
_MUSIC_ONLY = re.compile(r"^[\s\[\]\(\)\*~\-\u266a\u266b\u266c]+$|^\[?\s*(music|applause|laughter)\s*\]?$", re.I)


def _ts_to_ms(ts: str) -> int:
    """Convert a 'HH:MM:SS.mmm' / 'MM:SS.mmm' cue timestamp to milliseconds."""
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, s = parts
    else:
        h, m, s = "0", parts[0], parts[1]
    sec, _, frac = s.partition(".")
    ms = int((frac + "000")[:3]) if frac else 0
    return ((int(h) * 60 + int(m)) * 60 + int(sec)) * 1000 + ms


def _fmt_lrc(ms: int) -> str:
    """Format milliseconds as an LRC '[MM:SS.xx]' timestamp."""
    m, rem = divmod(int(ms), 60000)
    s, rem2 = divmod(rem, 1000)
    return f"[{m:02d}:{s:02d}.{rem2 // 10:02d}]"


def _parse_cues(text: str):
    """Split a VTT/SRT transcript into (start_ms, body) cues, in file order."""
    cues = []
    for block in re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.split("\n")
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ti is None:
            continue
        m = _CUE_TS.search(lines[ti].split("-->")[0])
        if not m:
            continue
        cues.append((_ts_to_ms(m.group(1)), "\n".join(lines[ti + 1:])))
    return cues


def _clean_cue_text(body: str) -> str:
    """Strip timing/style tags and HTML entities from a cue body, returning its display text."""
    body = _WORD_TS.sub("", body)
    body = _ANY_TAG.sub("", body)
    body = (body.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    return " ".join(ln.strip() for ln in body.split("\n") if ln.strip()).strip()


def _subs_to_lrc(text: str):
    """Convert a VTT/SRT transcript to an .lrc string (synced), or None if it has no usable lyrics.

    Auto captions carry inline word timings and repeat each line across rolling cues; a short
    "snapshot" cue (one without word timings) is emitted whenever a line finishes, so those give the
    clean text. Manual captions have no word timings at all, so every cue is used directly.
    """
    cues = _parse_cues(text)
    if not cues:
        return None
    is_auto = any(_WORD_TS.search(body) for _, body in cues)
    rows = []
    if is_auto:
        # Prefer the clean snapshot cues; fall back to the last line of each cue if none exist.
        for start, body in cues:
            if _WORD_TS.search(body):
                continue
            line = _clean_cue_text(body)
            if line and not _MUSIC_ONLY.match(line):
                rows.append((start, line))
        if not rows:
            for start, body in cues:
                line = _clean_cue_text(body).split("\n")[-1].strip()
                if line and not _MUSIC_ONLY.match(line):
                    rows.append((start, line))
    else:
        for start, body in cues:
            line = _clean_cue_text(body)
            if line and not _MUSIC_ONLY.match(line):
                rows.append((start, line))

    # Collapse the rolling duplicates of auto captions: a line that is a prefix of the next (or
    # vice versa) is the same caption growing word by word, so keep the longest text at the
    # earliest timestamp. Exact repeats and empty/music-only lines are dropped.
    out = []  # list of (start_ms, line)
    for start, line in rows:
        if out:
            p_start, p_line = out[-1]
            if line == p_line:
                continue
            if is_auto and (line.startswith(p_line) or p_line.startswith(line)):
                if len(line) >= len(p_line):
                    out[-1] = (p_start, line)
                continue
        out.append((start, line))
    return "\n".join(f"{_fmt_lrc(s)}{t}" for s, t in out)


def fetch_youtube_lyrics(url: str):
    """Fetch a video's captions via yt-dlp and convert them to .lrc. Returns the text or None.

    Downloads the manual and auto captions (English first, then the original language) into a
    temp dir, converts them with `_subs_to_lrc`, and returns the first usable result. Never raises.
    """
    if not url:
        return None
    import glob
    import os
    import tempfile

    import yt_dlp

    from .log import YtdlpLogger

    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en-orig", "en", "en-US", "en-GB", "orig"],
            "subtitlesformat": "vtt",
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        if verbose():
            opts.update({"verbose": True, "logger": YtdlpLogger(), "no_warnings": False})
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as e:
            log.debug("yt-dlp captions (%s) failed: %s", url, e, exc_info=verbose())
            return None

        files = sorted(glob.glob(os.path.join(tmp, "*.vtt")) + glob.glob(os.path.join(tmp, "*.srt")),
                       key=lambda p: os.path.basename(p).lower())
        for f in files:
            try:
                raw = open(f, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            lrc = _subs_to_lrc(raw)
            if lrc:
                return lrc
    return None


def fetch_lyrics(artist: str, title: str, duration: float = None, genre: str = None, album: str = None,
                 source_url: str = None):
    """Return (text, source) where text is plain or synced lyrics and source is a short label.

    Returns (None, reason) when no lyrics could be found. Never raises on network errors: it
    logs and returns a reason so the UI can report it without failing the whole batch.

    Nightcore / sped-up uploads are recognised by the genre, or by a marker in the artist or
    title, and handled accordingly: the speed markers are stripped from the title, an 'artist'
    that is really just a nightcore channel is dropped, the (shortened) duration is not trusted
    for the exact match, and a title-only fallback is allowed. That fallback only accepts a
    near-exact title, so it never returns a different song that merely shares a word.

    When the artist + title search comes up empty, the album is used as an extra clue: first
    title + album + artist, then title + album. As a final fallback, when `source_url` (the source
    link stored in the file's comment tag) points at a video, its YouTube captions are read and
    converted to .lrc - the last resort for non-official uploads the databases don't carry.
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

    # 2) Search. With a real artist, require it first (a title-only hit would likely be a different
    # song). The album is an extra clue when the artist-based search fails: title + album + artist,
    # then title + album. Finally, as a last resort, try the title alone: this is what rescues
    # non-official uploads (nightcore, fan covers, label/channel rips) whose "artist" matches no
    # record. It accepts only a near-exact title, so it still can't return a different song.
    attempts = []
    for name in artists:
        attempts.append(({"track_name": title, "artist_name": name}, None))
    if album and artists:
        attempts.append(({"track_name": title, "album_name": album, "artist_name": artists[0]}, None))
    if album:
        attempts.append(({"track_name": title, "album_name": album}, title))
    attempts.append(({"track_name": title}, title))
    attempts.append(({"q": title}, title))

    last_reason = "No match found."
    for params, exact in attempts:
        try:
            hits = _search(params)
        except Exception as e:
            log.debug("LRCLIB search (%s) failed: %s", params, e, exc_info=verbose())
            last_reason = "Could not reach the lyrics service."
            break

        best = _from_hits(hits, exact)
        if not best or best.get("instrumental"):
            if best:
                last_reason = "Track is marked instrumental."
            continue
        text = best.get("syncedLyrics") or best.get("plainLyrics")
        if text:
            return text, "search match"

    # 3) LRCLIB came up empty (or was unreachable): fall back to lrc.red before giving up.
    red = _lrc_red_lookup(artist, title, dur)
    if red:
        return red, "lrc.red"

    # 4) Still nothing: read the video's own captions from the source link kept in the file's
    # comment tag. For a non-official upload the databases don't have, its captions are the only
    # lyrics that actually match the audio.
    yt = fetch_youtube_lyrics(source_url)
    if yt:
        return yt, "youtube"
    return None, last_reason


def sidecar_path(path) -> "Path":
    """Return the .lrc file path that belongs to the given audio file path."""
    from pathlib import Path
    p = Path(path)
    return p.with_suffix(".lrc")


def save_lyrics(path, text: str, source: str = None) -> "Path":
    """Write the lyrics as a .lrc sidecar next to `path`. Returns the sidecar path.

    When the lyrics came from lrc.red, a credit line linking back to https://lrc.red is written as
    an LRC metadata tag at the top of the file, as the lrc.red API terms require.
    """
    lrc = sidecar_path(path)
    body = _as_lrc(text)
    if source == "lrc.red":
        body = f"[re:{_LRCRED_BASE}]\n{body}"
    lrc.write_text(body, encoding="utf-8")
    return lrc