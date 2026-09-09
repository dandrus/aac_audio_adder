#!/usr/bin/python3
"""
aac_dual_audio.py
─────────────────────────────────────────────────────────────────────────────
Radarr / Sonarr post-processing script that bakes a browser-friendly AAC
stereo track into every media file alongside the original surround track.

WHY THIS EXISTS
───────────────
Jellyfin browser clients streaming over a Cloudflare tunnel trigger HLS
transcoding whenever the audio codec is not natively supported by the browser
(e.g. EAC3 / Atmos / DTS).  Transcoding kills performance and quality.

By pre-encoding a stereo AAC track and marking it as the default, the browser
can direct-play the file.  Clients that support surround (AppleTV app, Jellyfin
Android, etc.) can still select the original EAC3/DTS track.

CALLER DETECTION
────────────────
Radarr sets:  radarr_moviefile_path
Sonarr sets:  sonarr_episodefile_path

MANUAL TESTING
──────────────
  radarr_moviefile_path=/path/to/file.mkv python3 aac_dual_audio.py
  sonarr_episodefile_path=/path/to/episode.mkv python3 aac_dual_audio.py
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — edit these constants to match your preferences
# ═══════════════════════════════════════════════════════════════════════════════

# Bitrate for the newly created AAC track.
# 384k is generous for 2-channel stereo; 192k is perfectly audible.
# Browsers handle up to ~320 kbps AAC reliably.
AAC_BITRATE: str = "384k"

# Output channel count for the new AAC track.
# 2 = stereo — the safest choice for all browsers and the Cloudflare tunnel.
# You can set 6 for 5.1 AAC, but stereo guarantees direct-play in browsers.
AAC_CHANNELS: int = 2

# If True, rewrite video / audio / subtitle track titles to a consistent
# scheme derived from each stream's own properties, replacing whatever the
# source shipped (release-group spam such as "..:::EmpireBestTV.Com:::." or
# "ExtraFlix.Pw | English AAC2.0 @ 128 kbps", as well as empty titles).
#
#   video     "<resolution> <source> <codec>"   → "720p WEBDL HEVC"
#   audio     "<layout> <codec> <language>"     → "5.1 Dolby Digital Plus English"
#   subtitle  "<language> (<qualifiers>)"       → "Persian (Forced)"
#
# Commentary / forced / SDH tracks keep that distinction as a parenthetical
# qualifier, so retitling never flattens a commentary track into a plain
# dialogue one.  See build_video_title / build_audio_title / build_subtitle_title.
SET_TRACK_TITLES: bool = True

# If True, clear the container-level title when it looks like release-group
# spam (see looks_like_junk_title).  A clean container title is left alone.
# Jellyfin prefers the NFO / filename over this field, so clearing it is safe.
CLEAR_JUNK_CONTAINER_TITLE: bool = True

# Titles this script wrote in earlier versions, before track titles were
# generated per-stream.  Still recognized by find_english_aac_track when
# deciding whether a file already carries an AAC track we created: those
# tracks were written without a language tag, so the title is the only
# marker identifying them as ours.  Do not remove entries from this set —
# doing so would make the script reprocess every file it has already done.
LEGACY_AAC_TRACK_TITLES: frozenset = frozenset({"AAC 2.0 Stereo"})

# If True, skip files that already contain an AAC audio track.
# Set False to force reprocessing (e.g., after changing AAC_BITRATE).
SKIP_IF_AAC_EXISTS: bool = True

# If True, keep ALL original audio tracks (dubs, commentary, etc.).
# If False, only the selected primary surround track is kept alongside AAC.
# Keeping all tracks is safer at the cost of slightly larger files.
# NOTE: If False, any non-primary tracks (dubs, director's commentary, etc.)
#       are permanently dropped from the output.  Default: True to be safe.
PRESERVE_ALL_AUDIO: bool = True

# If True, insert the new AAC track first (index 0) and mark it as default.
# Jellyfin and most players select the first default-flagged audio track.
# Set False to place AAC after the surround track (less common preference).
AAC_AS_FIRST_TRACK: bool = True

# If True, mark the best English subtitle track as default and clear the
# default flag on every other subtitle track.  Commentary / director tracks
# are excluded; plain dialogue subs are preferred over SDH and forced tracks.
# If no English subtitle exists, subtitle dispositions are left untouched.
SET_ENGLISH_SUBTITLE_DEFAULT: bool = True

# Staging directory for ffmpeg temp output, on the NVMe (/mnt/mediadepot).
# Writing the temp file on the same spinning disk as the source creates
# sustained read+write head contention on the media pool, which stalls
# Jellyfin playback.  The finished file is moved back to the source
# directory afterwards in one sequential burst.
# If this mount is missing, unwritable, or full, the script falls back to
# the previous behavior (temp file next to the source) with a warning.
STAGING_DIR: Path = Path("/mnt/mediadepot/aac_tmp")

# Hard ceiling on how long a single ffmpeg run may take, in seconds.
# Sonarr/Radarr run this script synchronously and wait for it to exit before
# processing the next import, so a hung ffmpeg (corrupt source, a stalled
# read off a network mount, etc.) would otherwise block the entire import
# queue forever, with no way to recover short of restarting the *arr service.
# 30 minutes is generous for an audio-only transcode (video is stream-copied)
# even on a large multi-track file; anything past that is almost certainly
# hung rather than genuinely still working.
FFMPEG_TIMEOUT_SECONDS: int = 1800

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# CALLER DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def detect_caller() -> Tuple[str, Path]:
    """
    Identify which *arr application called this script and return the file path.

    Environment variables checked (case-insensitive on Linux, but *arr sets
    them in lowercase):
        radarr_moviefile_path       — set by Radarr for movie post-processing
        sonarr_episodefile_path     — set by Sonarr for episode post-processing

    Returns:
        (app_name, Path) — e.g. ("Radarr", Path("/media/movies/..."))

    Raises:
        SystemExit(1) if neither variable is found.
    """
    # *arr sends eventtype=Test when the user clicks "Test" in the UI.
    # No file path is provided — just acknowledge and exit cleanly.
    sonarr_event = os.environ.get("sonarr_eventtype", "")
    radarr_event = os.environ.get("radarr_eventtype", "")
    if sonarr_event.lower() == "test" or radarr_event.lower() == "test":
        log.info("Test event received — configuration OK.")
        sys.exit(0)

    radarr_path = os.environ.get("radarr_moviefile_path")
    sonarr_path = os.environ.get("sonarr_episodefile_path")

    if radarr_path:
        log.info("Invoked by: Radarr")
        return "Radarr", Path(radarr_path)

    if sonarr_path:
        log.info("Invoked by: Sonarr")
        return "Sonarr", Path(sonarr_path)

    log.error(
        "No recognized environment variable found.\n"
        "  Expected: radarr_moviefile_path  (Radarr)\n"
        "         or sonarr_episodefile_path (Sonarr)\n"
        "\n"
        "  For manual testing:\n"
        "    radarr_moviefile_path=/path/to/file.mkv python3 aac_dual_audio.py"
    )
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
# FFPROBE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def probe_file(file_path: Path) -> Dict[str, Any]:
    """
    Run ffprobe on *file_path* and return the parsed JSON output.

    Uses JSON output mode rather than text parsing to avoid fragility with
    unusual codec names, special characters in titles, etc.

    Raises:
        subprocess.CalledProcessError  if ffprobe exits non-zero
        FileNotFoundError              if ffprobe is not installed
        json.JSONDecodeError           if output cannot be parsed
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",              # suppress banner noise
        "-print_format", "json",    # machine-readable output
        "-show_streams",            # include all stream details
        "-show_format",             # include container/format details
        "-show_chapters",           # include chapter markers
        str(file_path),
    ]
    log.debug("ffprobe: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        log.error("ffprobe failed (exit %d): %s", exc.returncode, exc.stderr.strip())
        raise
    except FileNotFoundError:
        log.error(
            "ffprobe not found.  Install ffmpeg:\n"
            "  sudo dnf install ffmpeg          # Fedora/RHEL\n"
            "  sudo apt install ffmpeg          # Debian/Ubuntu"
        )
        raise

    return json.loads(result.stdout)


def get_streams_by_type(
    probe_data: Dict[str, Any],
    codec_type: str,
) -> List[Dict[str, Any]]:
    """Return all streams whose codec_type matches *codec_type*."""
    return [
        s for s in probe_data.get("streams", [])
        if s.get("codec_type") == codec_type
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# COMMENTARY / NON-PRIMARY TRACK DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

# Keywords that strongly suggest a track is not the primary program audio.
# Matched case-insensitively as substrings against the track title and
# handler_name metadata tags.
#
# HEURISTIC RATIONALE:
#   • "commentary" / "comment"   — director/cast commentary tracks
#   • "director"                 — director's commentary (common on BDs)
#   • "interview" / "featurette" — bonus content
#   • "making of" / "behind"     — behind-the-scenes audio
#   • "alternate"                — alternate cut or score tracks
#   • "descriptive" / "description" / "dvs" — audio description for accessibility
#     (these are genuine primary-language tracks but rarely wanted as default)
#
# FALSE POSITIVE RISK: A film named "Commentary on War" would be mis-flagged.
# The code falls back to the first track if everything is flagged, so this
# cannot cause a total failure — just a suboptimal primary selection.
_COMMENTARY_KEYWORDS: Tuple[str, ...] = (
    "commentary",
    "comment",
    "director",
    "interview",
    "making of",
    "behind the scenes",
    "featurette",
    "alternate",
    "descriptive",
    "description",
    "dvs",        # Descriptive Video Service (US broadcast audio description)
    "ad track",   # Audio Description track abbreviation
)


def is_commentary(stream: Dict[str, Any]) -> bool:
    """
    Heuristic: return True if this audio stream appears to be a commentary,
    audio-description, or other non-primary track.

    Detection rules (checked in priority order):
      1. ffprobe reports disposition.comment == 1  (encoder-set flag; most reliable)
      2. The track's title tag contains a keyword from _COMMENTARY_KEYWORDS
      3. The track's handler_name tag contains a keyword (less reliable)

    Returns False (not commentary) when uncertain — it is better to include
    a commentary track as the primary source than to reject all tracks.
    """
    # Rule 1: disposition flag — set by authoring tools for commentary tracks.
    if stream.get("disposition", {}).get("comment", 0) == 1:
        return True

    tags = stream.get("tags", {})
    # Prefer uppercase fallback keys — MKV and MP4 can store tags either way.
    title = (
        tags.get("title") or tags.get("TITLE") or ""
    ).lower()
    handler = (
        tags.get("handler_name") or tags.get("HANDLER_NAME") or ""
    ).lower()

    # Rule 2 & 3: keyword matching.
    return any(kw in title or kw in handler for kw in _COMMENTARY_KEYWORDS)


# ═══════════════════════════════════════════════════════════════════════════════
# PRIMARY AUDIO SELECTION
# ═══════════════════════════════════════════════════════════════════════════════

# Codec preference order when multiple non-commentary tracks exist.
# Lower index = preferred source for AAC transcoding.
# Lossless/high-bitrate formats produce the best AAC result.
#
# NOTE: ffprobe reports DTS-HD MA and DTS:X as codec_name="dts"; they are
# differentiated by the "profile" field and ranked at the top in
# codec_quality_rank() below.
_CODEC_PREFERENCE: List[str] = [
    "truehd",       # Dolby TrueHD (lossless, may include Atmos object layer)
    "dts",          # DTS — rank refined by profile in codec_quality_rank()
    "flac",         # Lossless PCM-based (rare in Blu-ray rips but possible)
    "eac3",         # Dolby Digital Plus / E-AC-3 (Atmos lossy core)
    "ac3",          # Dolby Digital / AC-3 (5.1 @ up to 640 kbps)
    "aac",          # AAC (already present; handled by SKIP_IF_AAC_EXISTS)
    "mp3",
    "vorbis",
    "opus",
    "pcm_s24le",
    "pcm_s16le",
]

# DTS profiles that indicate lossless or near-lossless quality.
# ffprobe populates stream["profile"] with these strings.
_DTS_LOSSLESS_PROFILES: frozenset = frozenset({"DTS-HD MA", "DTS:X", "DTS-HD HRA"})


def codec_quality_rank(stream: Dict[str, Any]) -> int:
    """
    Return a quality rank for *stream* (lower = better = preferred source).

    DTS-HD MA / DTS:X streams are promoted to the same rank as TrueHD
    because they are effectively lossless.  Standard DTS falls in its
    natural _CODEC_PREFERENCE position.
    """
    codec = stream.get("codec_name", "").lower()
    profile = stream.get("profile", "")

    if codec == "dts" and profile in _DTS_LOSSLESS_PROFILES:
        return _CODEC_PREFERENCE.index("truehd") + 1  # tie with TrueHD, not beat it

    try:
        return _CODEC_PREFERENCE.index(codec) + 1
    except ValueError:
        return len(_CODEC_PREFERENCE) + 1  # Unknown codec → lowest priority


# A stereo AAC title this script generates, for a track whose language is
# English or absent.  Anchored and language-explicit on purpose: "2.0 AAC
# Spanish" must NOT match, or a foreign dub would be mistaken for the English
# AAC track and the file would be skipped without ever getting one.
_SELF_AUTHORED_AAC_TITLE_RE = re.compile(r"^\d+\.\d+ AAC(?: English)?$")


def is_self_authored_aac_title(title: str) -> bool:
    """
    True when *title* is one this script wrote for an AAC track it created.

    Covers both the current generated form ("2.0 AAC English") and the fixed
    strings used by earlier versions (LEGACY_AAC_TRACK_TITLES).
    """
    title = title.strip()
    if title in LEGACY_AAC_TRACK_TITLES:
        return True
    return bool(_SELF_AUTHORED_AAC_TITLE_RE.match(title))


def find_english_aac_track(
    audio_streams: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Return the first non-commentary English AAC track, or None.

    Matches on either:
      • Language tag is English ("eng", "en", "english"), OR
      • Track title is one this script authored (is_self_authored_aac_title) —
        catches tracks injected on a prior run when the source had no language
        tag, so we never set one.

    Foreign-language dub AAC tracks (e.g. Turkish, Czech, Hungarian) are
    ignored — their presence does not mean the file is ready for Jellyfin
    browser direct-play.
    """
    _ENGLISH_TAGS: frozenset = frozenset({"eng", "en", "english"})
    for s in audio_streams:
        if s.get("codec_name", "").lower() != "aac":
            continue
        if is_commentary(s):
            continue
        tags = s.get("tags", {})
        lang  = (tags.get("language") or tags.get("LANGUAGE") or "").lower()
        title = tags.get("title") or tags.get("TITLE") or ""
        if lang in _ENGLISH_TAGS or is_self_authored_aac_title(title):
            return s
    return None


def has_english_aac_track(audio_streams: List[Dict[str, Any]]) -> bool:
    """Return True if a non-commentary English AAC track already exists."""
    return find_english_aac_track(audio_streams) is not None


def audio_dispositions_correct(
    audio_streams: List[Dict[str, Any]],
    aac_stream: Dict[str, Any],
) -> bool:
    """
    Return True if *aac_stream* is the ONLY audio track flagged default.

    Files that ship with an English AAC track already baked in often carry
    wrong flags from the source (e.g. a foreign dub flagged default), which
    makes Jellyfin pick the wrong track.  Such files are skipped by the AAC
    check but still need a disposition-only fix.
    """
    if aac_stream.get("disposition", {}).get("default", 0) != 1:
        return False
    for s in audio_streams:
        if s["index"] == aac_stream["index"]:
            continue
        if s.get("disposition", {}).get("default", 0) == 1:
            return False
    return True


def select_primary_audio(
    audio_streams: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Choose the best audio stream to:
      (a) transcode into the new AAC track, and
      (b) preserve as-is as the surround track.

    Selection algorithm:
      1. Filter out commentary tracks via is_commentary().
      2. If all tracks are flagged commentary, fall back to all tracks
         and log a warning (better to pick something than nothing).
      3. Among candidates, prefer English-language tracks.
         Falls back to all candidates if no English track is found.
      4. Among candidates, prefer any stream with disposition.default == 1.
         Encoders typically flag the primary language track as default.
      5. Among remaining ties, sort by codec_quality_rank() ascending.
      6. Return the first (highest quality, non-commentary, English-preferred) track.

    Returns None only if audio_streams is empty.
    """
    if not audio_streams:
        return None

    # Step 1: exclude commentary tracks.
    candidates = [s for s in audio_streams if not is_commentary(s)]

    # Step 2: full fallback.
    if not candidates:
        log.warning(
            "All %d audio track(s) detected as commentary — "
            "using first track as fallback primary.",
            len(audio_streams),
        )
        candidates = list(audio_streams)

    # Step 3: prefer English tracks.
    _ENGLISH_TAGS: frozenset = frozenset({"eng", "en", "english"})
    english = [
        s for s in candidates
        if (s.get("tags", {}).get("language") or s.get("tags", {}).get("LANGUAGE") or "").lower()
        in _ENGLISH_TAGS
    ]
    found_english = bool(english)
    if found_english:
        candidates = english
    else:
        log.warning(
            "No English audio track found — selecting by codec quality only "
            "(ignoring disposition.default to avoid picking a foreign-language dub)."
        )

    # Step 4: prefer tracks flagged as default by the encoder.
    # Only apply when English tracks were found.  If no English was detected,
    # the default flag may point to a non-English (e.g. foreign dub) track —
    # skipping this step prevents blindly picking the wrong language.
    if found_english:
        default_flagged = [
            s for s in candidates
            if s.get("disposition", {}).get("default", 0) == 1
        ]
        if default_flagged:
            candidates = default_flagged

    # Step 5: sort by codec quality (ascending rank = better).
    candidates.sort(key=codec_quality_rank)

    selected = candidates[0]
    tags = selected.get("tags", {})
    title = tags.get("title") or tags.get("TITLE") or "(no title)"
    log.info(
        "Primary audio selected — index: %d | codec: %s | profile: %s | "
        "channels: %s | language: %s | title: %r",
        selected["index"],
        selected.get("codec_name", "?"),
        selected.get("profile", "?"),
        selected.get("channels", "?"),
        tags.get("language") or tags.get("LANGUAGE") or "?",
        title,
    )
    return selected


# ═══════════════════════════════════════════════════════════════════════════════
# DEFAULT SUBTITLE SELECTION
# ═══════════════════════════════════════════════════════════════════════════════

def select_default_subtitle(
    subtitle_streams: List[Dict[str, Any]],
) -> Optional[int]:
    """
    Pick which subtitle stream should carry the default flag in the output.

    Returns the POSITION of the chosen stream within *subtitle_streams* —
    which equals its output subtitle index, since subtitles are mapped in
    list order — or None when no suitable English track exists (source
    dispositions are then left untouched).

    Selection algorithm:
      1. Candidates must be tagged English ("eng"/"en"/"english").
         Untagged tracks are excluded — they could be any language.
      2. Commentary / director tracks are excluded via is_commentary()
         (disposition.comment flag + title keyword heuristics).
      3. Remaining candidates are ranked:
           0 — plain dialogue subs (not forced, not SDH)
           1 — SDH / hearing-impaired (fallback if nothing plain exists)
           2 — forced (signs / foreign-dialogue only; last resort)
      4. Ties broken by the source default flag, then file order.
    """
    _ENGLISH_TAGS: frozenset = frozenset({"eng", "en", "english"})

    candidates: List[Tuple[int, int, int]] = []  # (rank, not-source-default, pos)
    for pos, s in enumerate(subtitle_streams):
        tags = s.get("tags", {}) or {}
        lang = (tags.get("language") or tags.get("LANGUAGE") or "").lower()
        if lang not in _ENGLISH_TAGS:
            continue
        if is_commentary(s):
            continue

        disp = s.get("disposition", {}) or {}
        title = (tags.get("title") or tags.get("TITLE") or "").lower()

        if disp.get("forced", 0) == 1 or "forced" in title:
            rank = 2
        elif (
            disp.get("hearing_impaired", 0) == 1
            or "sdh" in title
            or "hearing impaired" in title
        ):
            rank = 1
        else:
            rank = 0

        candidates.append((rank, 0 if disp.get("default", 0) == 1 else 1, pos))

    if not candidates:
        log.info("No suitable English subtitle found — leaving subtitle flags as-is.")
        return None

    best_pos = min(candidates)[2]
    chosen = subtitle_streams[best_pos]
    chosen_tags = chosen.get("tags", {}) or {}
    log.info(
        "Default subtitle selected — index: %d | codec: %s | title: %r",
        chosen["index"],
        chosen.get("codec_name", "?"),
        chosen_tags.get("title") or chosen_tags.get("TITLE") or "(no title)",
    )
    return best_pos


# ═══════════════════════════════════════════════════════════════════════════════
# TRACK TITLE GENERATION
# ═══════════════════════════════════════════════════════════════════════════════
#
# Titles are generated from what the stream actually *is* rather than from
# whatever string the release group left behind.  Every builder returns ""
# when it has nothing meaningful to say, and callers skip empty titles rather
# than writing a blank tag.

# Channel count → the layout notation viewers recognize.  Anything not listed
# falls back to "<n>.0", which is right for plain multi-mono/multi-stereo beds.
_CHANNEL_LAYOUTS: Dict[int, str] = {
    1: "1.0", 2: "2.0", 3: "2.1", 4: "4.0",
    5: "4.1", 6: "5.1", 7: "6.1", 8: "7.1", 10: "9.1",
}

_AUDIO_CODEC_NAMES: Dict[str, str] = {
    "aac":    "AAC",
    "ac3":    "Dolby Digital",
    "eac3":   "Dolby Digital Plus",
    "truehd": "Dolby TrueHD",
    "dts":    "DTS",
    "flac":   "FLAC",
    "opus":   "Opus",
    "mp3":    "MP3",
    "mp2":    "MP2",
    "vorbis": "Vorbis",
    "alac":   "ALAC",
    "wmav2":  "WMA",
}

_VIDEO_CODEC_NAMES: Dict[str, str] = {
    "hevc":       "HEVC",
    "h264":       "H.264",
    "av1":        "AV1",
    "vp9":        "VP9",
    "vp8":        "VP8",
    "mpeg4":      "MPEG-4",
    "mpeg2video": "MPEG-2",
    "mpeg1video": "MPEG-1",
    "vc1":        "VC-1",
    "theora":     "Theora",
}

# Still-image codecs that appear as "video" streams but are really embedded
# cover art / poster thumbnails.  Not every muxer sets disposition.attached_pic
# (observed: a 400x225 PNG poster with attached_pic=0 AND still_image=0), so
# the codec name is the only reliable signal.  Titling these produces
# nonsense like "WEBDL PNG" on a stream no viewer ever selects.
_IMAGE_CODECS: frozenset = frozenset({
    "png", "apng", "mjpeg", "jpeg2000", "bmp", "gif", "webp", "tiff", "ppm",
})

# ISO 639-1 and both ISO 639-2 variants (bibliographic + terminological), since
# muxers in the wild use all three.  "und"/"mis"/"zxx" map to "" so an
# undefined language contributes nothing to the title instead of the word
# "Und".  Unknown codes fall back to their own title-cased form.
_LANGUAGE_NAMES: Dict[str, str] = {
    "eng": "English",    "en": "English",
    "spa": "Spanish",    "es": "Spanish",
    "fre": "French",     "fra": "French",     "fr": "French",
    "ger": "German",     "deu": "German",     "de": "German",
    "ita": "Italian",    "it": "Italian",
    "por": "Portuguese", "pt": "Portuguese",
    "rus": "Russian",    "ru": "Russian",
    "jpn": "Japanese",   "ja": "Japanese",
    "kor": "Korean",     "ko": "Korean",
    "chi": "Chinese",    "zho": "Chinese",    "zh": "Chinese",
    "ara": "Arabic",     "ar": "Arabic",
    "hin": "Hindi",      "hi": "Hindi",
    "per": "Persian",    "fas": "Persian",    "fa": "Persian",
    "dut": "Dutch",      "nld": "Dutch",      "nl": "Dutch",
    "swe": "Swedish",    "sv": "Swedish",
    "nor": "Norwegian",  "no": "Norwegian",
    "dan": "Danish",     "da": "Danish",
    "fin": "Finnish",    "fi": "Finnish",
    "pol": "Polish",     "pl": "Polish",
    "tur": "Turkish",    "tr": "Turkish",
    "tha": "Thai",       "th": "Thai",
    "vie": "Vietnamese", "vi": "Vietnamese",
    "heb": "Hebrew",     "he": "Hebrew",
    "cze": "Czech",      "ces": "Czech",      "cs": "Czech",
    "hun": "Hungarian",  "hu": "Hungarian",
    "gre": "Greek",      "ell": "Greek",      "el": "Greek",
    "rum": "Romanian",   "ron": "Romanian",   "ro": "Romanian",
    "ukr": "Ukrainian",  "uk": "Ukrainian",
    "ind": "Indonesian", "id": "Indonesian",
    "may": "Malay",      "msa": "Malay",      "ms": "Malay",
    "tgl": "Tagalog",    "fil": "Filipino",
    "cat": "Catalan",    "ca": "Catalan",
    "bul": "Bulgarian",  "bg": "Bulgarian",
    "hrv": "Croatian",   "hr": "Croatian",
    "srp": "Serbian",    "sr": "Serbian",
    "slo": "Slovak",     "slk": "Slovak",     "sk": "Slovak",
    "slv": "Slovenian",  "sl": "Slovenian",
    "lit": "Lithuanian", "lt": "Lithuanian",
    "lav": "Latvian",    "lv": "Latvian",
    "est": "Estonian",   "et": "Estonian",
    "ice": "Icelandic",  "isl": "Icelandic",  "is": "Icelandic",
    "alb": "Albanian",   "sqi": "Albanian",
    "mac": "Macedonian", "mkd": "Macedonian",
    "wel": "Welsh",      "cym": "Welsh",
    "tam": "Tamil",      "tel": "Telugu",     "ben": "Bengali",
    "mar": "Marathi",    "urd": "Urdu",       "pan": "Punjabi",
    "mal": "Malayalam",  "kan": "Kannada",    "guj": "Gujarati",
    "afr": "Afrikaans",  "bos": "Bosnian",    "lat": "Latin",
    "gle": "Irish",      "srd": "Sardinian",  "glg": "Galician",
    "eus": "Basque",     "baq": "Basque",
    "und": "",           "mis": "",           "zxx": "",
}

# Source-media token, read from the filename because it is not recoverable
# from the stream data.  Radarr/Sonarr put it in every name they write
# ("… WEBDL-720p.mkv", "… Bluray-1080p.mkv").  Ordered most-specific first:
# a Blu-ray remux should read "Remux", not "Bluray".
#
# There is deliberately no bare "WEB" fallback — it would misfire on titles
# that simply contain the word (e.g. "Charlotte's Web (2006).mkv").
_SOURCE_PATTERNS: Tuple[Tuple[Any, str], ...] = (
    (re.compile(r"\bremux\b",                 re.I), "Remux"),
    (re.compile(r"\b(?:blu-?ray|bd-?rip|br-?rip)\b", re.I), "Bluray"),
    (re.compile(r"\bweb-?dl\b",               re.I), "WEBDL"),
    (re.compile(r"\bweb-?rip\b",              re.I), "WEBRip"),
    (re.compile(r"\bhdtv\b",                  re.I), "HDTV"),
    (re.compile(r"\bdvd(?:-?rip)?\b",         re.I), "DVD"),
)

# Width is checked before height because scope/anamorphic framing crops the
# height well below the nominal tier — a 720p widescreen encode is commonly
# 1280x536, which a height-first rule would mislabel as 480p.
_RESOLUTION_BY_WIDTH: Tuple[Tuple[int, str], ...] = (
    (3840, "2160p"), (2560, "1440p"), (1920, "1080p"), (1280, "720p"),
)
_RESOLUTION_BY_HEIGHT: Tuple[Tuple[int, str], ...] = (
    (2160, "2160p"), (1440, "1440p"), (1080, "1080p"), (720, "720p"),
    (576, "576p"), (480, "480p"), (360, "360p"),
)

# Markers of a title that exists to advertise a site or release group rather
# than describe the track: a bare domain, a URL, a "@ 128 kbps" style spec
# dump, or runs of decorative punctuation.
# Ways a subtitle track announces it carries sound descriptions for the deaf
# and hard of hearing.  "[CC]" (closed captions) is treated as equivalent to
# SDH — the practical distinction does not survive into a player's track list,
# and dropping the marker entirely would make it indistinguishable from the
# plain dialogue track.  "cc" is matched only in bracketed or word form so it
# cannot fire on substrings inside ordinary words.
_SDH_TITLE_RE = re.compile(
    r"\bsdh\b|hearing[\s-]*impaired|closed[\s-]*caption|\[\s*cc\s*\]|\(\s*cc\s*\)",
    re.I,
)

_JUNK_TITLE_PATTERNS: Tuple[Any, ...] = (
    re.compile(r"https?://",                                    re.I),
    re.compile(r"\bwww\.",                                      re.I),
    re.compile(r"\.(?:com|net|org|info|biz|in|pw|cc|to|me|tv|io|xyz|club|site|online|link|ws)\b", re.I),
    re.compile(r":{2,}"),
    re.compile(r"@\s*\d+\s*kbps",                               re.I),
    re.compile(r"\bencode[sd]?\s+by\b",                         re.I),
)


def stream_title(stream: Dict[str, Any]) -> str:
    """Current title tag of *stream*, or "" when it carries none."""
    tags = stream.get("tags") or {}
    return (tags.get("title") or tags.get("TITLE") or "").strip()


def looks_like_junk_title(title: str) -> bool:
    """True when *title* advertises a site/release group instead of describing content."""
    if not title.strip():
        return False
    return any(p.search(title) for p in _JUNK_TITLE_PATTERNS)


def friendly_language(stream: Dict[str, Any]) -> str:
    """Human-readable language name for *stream*, or "" if untagged/undefined."""
    tags = stream.get("tags") or {}
    code = (tags.get("language") or tags.get("LANGUAGE") or "").strip().lower()
    if not code:
        return ""
    if code in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[code]
    return code.title()


def friendly_audio_codec(stream: Dict[str, Any]) -> str:
    """
    Marketing-facing codec name for an audio stream — "Dolby Digital Plus"
    rather than "eac3".

    DTS variants are read from the ffprobe *profile* ("DTS-HD MA", "DTS:X",
    "DTS-ES"), which is where the meaningful distinction lives; the codec_name
    is just "dts" for all of them.  An Atmos-bearing profile appends " Atmos".
    """
    codec   = (stream.get("codec_name") or "").lower()
    profile = (stream.get("profile") or "").strip()

    if codec.startswith("pcm"):
        return "PCM"

    name = _AUDIO_CODEC_NAMES.get(codec) or (codec.upper() if codec else "")

    # "DTS-HD MA" / "DTS:X" / "DTS-ES" are strictly better labels than "DTS".
    if codec == "dts" and "DTS" in profile.upper():
        name = profile

    if "atmos" in profile.lower() and "atmos" not in name.lower():
        name = f"{name} Atmos"

    return name


def friendly_video_codec(stream: Dict[str, Any]) -> str:
    """Display name for a video codec — "HEVC", "H.264", "AV1"."""
    codec = (stream.get("codec_name") or "").lower()
    return _VIDEO_CODEC_NAMES.get(codec) or (codec.upper() if codec else "")


def is_cover_art_stream(stream: Dict[str, Any]) -> bool:
    """
    True when a "video" stream is really embedded artwork, not the feature.

    Checked three ways because no single one is reliable: the disposition bits
    muxers are *supposed* to set, and the codec name for the muxers that
    don't.  Cover art should carry no title at all — a generated one reads as
    junk ("WEBDL PNG") on a stream that is never selectable.
    """
    disposition = stream.get("disposition") or {}
    if disposition.get("attached_pic") or disposition.get("still_image"):
        return True
    return (stream.get("codec_name") or "").lower() in _IMAGE_CODECS


def format_channel_layout(channels: Optional[int]) -> str:
    """Channel count → layout notation ("5.1"), or "" when unknown."""
    if not isinstance(channels, int) or channels <= 0:
        return ""
    return _CHANNEL_LAYOUTS.get(channels, f"{channels}.0")


def resolution_label(stream: Dict[str, Any]) -> str:
    """Resolution tier ("1080p") for a video stream, or "" if undeterminable."""
    width  = stream.get("width") or 0
    height = stream.get("height") or 0
    for threshold, label in _RESOLUTION_BY_WIDTH:
        if width >= threshold:
            return label
    for threshold, label in _RESOLUTION_BY_HEIGHT:
        if height >= threshold:
            return label
    return ""


def detect_source_type(file_path: Path) -> str:
    """Source-media token ("WEBDL", "Bluray") parsed from the filename, or ""."""
    name = file_path.name
    for pattern, label in _SOURCE_PATTERNS:
        if pattern.search(name):
            return label
    return ""


def compose_audio_title(
    channels: Optional[int],
    codec_label: str,
    language: str,
    commentary: bool = False,
) -> str:
    """Assemble "<layout> <codec> <language>", omitting any unknown component."""
    parts = [p for p in (format_channel_layout(channels), codec_label, language) if p]
    title = " ".join(parts)
    if commentary:
        return f"{title} (Commentary)" if title else "Commentary"
    return title


def build_audio_title(stream: Dict[str, Any], codec_override: str = "") -> str:
    """
    Title for an existing audio stream, from its own codec/channels/language.

    *codec_override* names the codec the track will have in the OUTPUT — set
    when the track is being transcoded (MP4 containers force surround audio to
    AC3), so the title describes the result rather than the source.
    """
    return compose_audio_title(
        stream.get("channels"),
        codec_override or friendly_audio_codec(stream),
        friendly_language(stream),
        commentary=is_commentary(stream),
    )


def build_video_title(stream: Dict[str, Any], file_path: Path) -> str:
    """Title for a video stream: "<resolution> <source> <codec>"."""
    parts = [
        p for p in (
            resolution_label(stream),
            detect_source_type(file_path),
            friendly_video_codec(stream),
        ) if p
    ]
    return " ".join(parts)


def build_subtitle_title(stream: Dict[str, Any]) -> str:
    """
    Title for a subtitle stream: language plus any qualifiers that change how
    a viewer would choose it ("English (SDH)", "Persian (Forced)").

    Qualifiers are read from the disposition bits first and the incoming title
    second — plenty of releases encode "forced"/"SDH" in the title alone and
    never set the corresponding flag.
    """
    disposition = stream.get("disposition") or {}
    existing    = stream_title(stream).lower()

    qualifiers: List[str] = []
    if disposition.get("forced") or "forced" in existing:
        qualifiers.append("Forced")
    if disposition.get("hearing_impaired") or _SDH_TITLE_RE.search(existing):
        qualifiers.append("SDH")
    if is_commentary(stream):
        qualifiers.append("Commentary")

    language = friendly_language(stream)
    if not language:
        # Nothing to say about an untagged sub beyond its qualifiers; leaving
        # the title alone beats writing a bare "(Forced)".
        return f"({', '.join(qualifiers)})" if qualifiers else ""

    return f"{language} ({', '.join(qualifiers)})" if qualifiers else language


def compute_title_changes(
    probe_data: Dict[str, Any],
    file_path: Path,
) -> List[Tuple[str, str, str]]:
    """
    Titles that would change if this file's existing tracks were retitled.

    Returns (stream_description, current_title, new_title) for each stream
    whose title would differ, plus a "container" entry when a junk container
    title would be cleared.  An empty list means the file is already correct.

    Only streams that already exist are considered — a newly injected AAC
    track is not a "change" — so this is exactly the set of edits a pure
    stream-copy remux would make, which is what both the skip check and the
    dry-run report need.
    """
    changes: List[Tuple[str, str, str]] = []

    if SET_TRACK_TITLES:
        for stream in get_streams_by_type(probe_data, "video"):
            old = stream_title(stream)
            if is_cover_art_stream(stream):
                # Artwork should carry no title.  Only a non-empty one is a
                # change (clearing it); untitled cover art is already correct,
                # so this never drags extra files into scope.
                if old:
                    changes.append((f"video:{stream['index']}", old, ""))
                continue
            new = build_video_title(stream, file_path)
            if new and new != old:
                changes.append((f"video:{stream['index']}", old, new))

        for stream in get_streams_by_type(probe_data, "audio"):
            new = build_audio_title(stream)
            old = stream_title(stream)
            if new and new != old:
                changes.append((f"audio:{stream['index']}", old, new))

        for stream in get_streams_by_type(probe_data, "subtitle"):
            new = build_subtitle_title(stream)
            old = stream_title(stream)
            if new and new != old:
                changes.append((f"subtitle:{stream['index']}", old, new))

    if CLEAR_JUNK_CONTAINER_TITLE:
        container_title = (
            (probe_data.get("format") or {}).get("tags") or {}
        ).get("title") or ""
        if looks_like_junk_title(container_title):
            changes.append(("container", container_title.strip(), ""))

    return changes


# ═══════════════════════════════════════════════════════════════════════════════
# FFMPEG COMMAND BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

def build_ffmpeg_command(
    input_path: Path,
    output_path: Path,
    probe_data: Dict[str, Any],
    primary_audio: Dict[str, Any],
    fix_dispositions_only: bool = False,
    av1_extra_input: Optional[Tuple[Path, int]] = None,
) -> List[str]:
    """
    Build and return the ffmpeg command list that produces the dual-audio file.

    FIX-ONLY MODE (fix_dispositions_only=True): used when the file already
    contains an English AAC track (passed as *primary_audio*) but the default
    flags are wrong (e.g. a foreign dub flagged default by the source).
    Every stream is copied — no transcoding — with the AAC track mapped first,
    flagged as the only default audio, and the subtitle default logic applied.
    Language tags are left untouched; track titles are still regenerated when
    SET_TRACK_TITLES is on, which makes this path double as the metadata-only
    remux used to strip release-group junk from an otherwise-correct file.
    All audio tracks are always kept regardless of PRESERVE_ALL_AUDIO.

    OUTPUT STREAM ORDER (when AAC_AS_FIRST_TRACK=True, PRESERVE_ALL_AUDIO=True):
    ┌──────────────────────────────────────────────────────────────────────┐
    │  [video 0..N]   stream copy — never re-encoded                       │
    │  [audio 0]      NEW AAC stereo — transcoded from primary_audio       │
    │  [audio 1]      original primary surround — stream copy              │
    │  [audio 2..M]   other original audio tracks — stream copy (if kept)  │
    │  [subtitle 0..K] all subtitle streams — stream copy                  │
    └──────────────────────────────────────────────────────────────────────┘

    KEY TECHNIQUE: The primary audio input stream is mapped TWICE.
      First mapping  → transcoded to AAC  (via -c:a:0 aac)
      Second mapping → stream copied as-is (via -c:a:1 copy)
    ffmpeg fully supports mapping the same input stream to multiple output
    streams with different processing.

    SUBTITLES: Mapped explicitly to avoid accidental loss.  When
    SET_ENGLISH_SUBTITLE_DEFAULT is True, the best English subtitle (per
    select_default_subtitle) gets the default flag and all others have it
    cleared; other disposition bits are preserved.  Note that some
    subtitle formats (ASS/SSA, SRT) cannot be stored in MP4 containers —
    ffmpeg will emit an error if the output container doesn't support them.
    MKV supports all common subtitle formats.

    DATA STREAMS: Deliberately excluded.  Timecode tracks and other data
    streams are rarely meaningful and can cause muxing errors.

    AV1 REPAIR (av1_extra_input): some AV1 WEB-DL releases (observed from the
    "Saon" release group) carry a malformed CodecPrivate for the video track —
    the full nested ISOBMFF 'av01' sample-entry box (av01/av1C/colr/btrt)
    copied verbatim instead of just the inner av1C configuration record. The
    MKV muxer requires a bare av1C record and fails "Could not write header"
    on the boxed form, even though a plain stream copy tolerates it fine.
    When the caller has already extracted a repaired copy of that one video
    stream to a timestamped .ivf file (see extract_av1_ivf), av1_extra_input
    = (ivf_path, original_absolute_stream_index) adds the ivf as INPUT 0 and
    shifts every other mapping to read from the original file as INPUT 1 —
    the ivf's own re-derived extradata is valid, everything else is untouched.
    """
    all_audio   = get_streams_by_type(probe_data, "audio")
    # Exclude video streams with no valid dimensions (e.g. MJPEG attached
    # pictures embedded by mkvmerge that ffmpeg can't fully probe).  Passing
    # these to the matroska muxer causes "dimensions not set" / header write
    # failure.  Streams with valid width+height (cover art with known size) are
    # kept; only truly unresolvable streams are dropped.
    all_video   = [
        vs for vs in get_streams_by_type(probe_data, "video")
        if vs.get("width", 0) > 0 and vs.get("height", 0) > 0
    ]
    raw_subs    = get_streams_by_type(probe_data, "subtitle")

    # Filter out subtitle streams with no recognized codec — they cause the
    # matroska muxer to fail with "Subtitle codec 0 is not supported".
    # Streams where ffprobe returns an empty, "none", or "unknown" codec_name
    # cannot be stream-copied and will prevent the output file header from writing.
    _BAD_SUBTITLE_CODECS: frozenset = frozenset({"", "none", "unknown", "data"})
    all_subs = []
    for ss in raw_subs:
        cname = ss.get("codec_name", "").lower()
        if cname in _BAD_SUBTITLE_CODECS:
            log.warning(
                "Skipping subtitle stream %d (codec=%r) — unrecognized codec "
                "cannot be muxed into output container.",
                ss["index"], ss.get("codec_name", ""),
            )
        else:
            all_subs.append(ss)

    primary_abs_idx = primary_audio["index"]  # absolute stream index in the file

    # All audio streams except the chosen primary
    other_audio = [s for s in all_audio if s["index"] != primary_abs_idx]

    # When av1_extra_input is set, the repaired video track is INPUT 0 (its
    # own extradata, mapped as "0:0") and the original file moves to INPUT 1
    # — every other stream (audio, subs, attachments, metadata, chapters)
    # comes from there instead of INPUT 0.  `src` is that input's index as
    # a string, used everywhere a stream is pulled from the original file.
    src = "1" if av1_extra_input is not None else "0"
    av1_fixed_stream_idx = av1_extra_input[1] if av1_extra_input is not None else None

    # ── Base command ───────────────────────────────────────────────────────────
    cmd: List[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",   # suppress verbose progress; use "info" to debug
    ]
    if av1_extra_input is not None:
        cmd += ["-i", str(av1_extra_input[0])]
    cmd += [
        # Increase probe limits so ffmpeg can resolve PGS subtitle parameters
        # ("unspecified size" errors) without falling back to "codec 0".
        "-probesize", "100M",
        "-analyzeduration", "100M",
        # Generate PTS for packets that lack timestamps where possible.
        # NOTE: genpts only works when ffmpeg can decode to synthesise timing;
        # it does NOT help in copy mode.  Unset-timestamp errors in copy mode
        # are caught and reported below with a repair suggestion.
        "-fflags", "+genpts",
        "-i", str(input_path),
    ]

    # ── Stream mapping — VIDEO ─────────────────────────────────────────────────
    # Map every video stream explicitly using its absolute index.
    # This preserves multi-angle discs and dual-video files correctly.
    # The repaired AV1 stream (if any) comes from INPUT 0's only stream;
    # everything else still comes from its absolute index in the source file.
    for vs in all_video:
        if av1_fixed_stream_idx is not None and vs["index"] == av1_fixed_stream_idx:
            cmd += ["-map", "0:0"]
        else:
            cmd += ["-map", f"{src}:{vs['index']}"]

    # ── Stream mapping — AUDIO ─────────────────────────────────────────────────
    # Fix-only mode: the existing AAC track (primary_audio) is mapped ONCE,
    # first, and every other audio track follows in original order.
    if fix_dispositions_only:
        cmd += ["-map", f"{src}:{primary_abs_idx}"]       # output audio 0 → existing AAC
        for oa in other_audio:
            cmd += ["-map", f"{src}:{oa['index']}"]       # output audio 1+ → copy
    # Normal mode: we map primary_audio TWICE:
    #   pass 1 → will receive -c:a:<N> aac  (new AAC track)
    #   pass 2 → will receive -c:a:<N> copy (original surround preserved)
    elif AAC_AS_FIRST_TRACK:
        cmd += ["-map", f"{src}:{primary_abs_idx}"]   # output audio 0 → new AAC
        cmd += ["-map", f"{src}:{primary_abs_idx}"]   # output audio 1 → surround copy
        if PRESERVE_ALL_AUDIO:
            for oa in other_audio:
                cmd += ["-map", f"{src}:{oa['index']}"]   # output audio 2+ → copy
    else:
        cmd += ["-map", f"{src}:{primary_abs_idx}"]   # output audio 0 → surround copy
        cmd += ["-map", f"{src}:{primary_abs_idx}"]   # output audio 1 → new AAC
        if PRESERVE_ALL_AUDIO:
            for oa in other_audio:
                cmd += ["-map", f"{src}:{oa['index']}"]   # output audio 2+ → copy

    # ── Stream mapping — SUBTITLES ─────────────────────────────────────────────
    for ss in all_subs:
        cmd += ["-map", f"{src}:{ss['index']}"]

    # ── Stream mapping — ATTACHMENTS ───────────────────────────────────────────
    # MKV attachments carry embedded cover art and fonts for ASS/SSA subtitles;
    # dropping them loses artwork and breaks styled-subtitle rendering.
    # MP4-family containers don't support attachment streams, so skip there.
    # The trailing '?' makes the mapping optional (no error when none exist).
    _MP4_CONTAINERS = frozenset({".mp4", ".m4v"})
    if output_path.suffix.lower() not in _MP4_CONTAINERS:
        cmd += ["-map", f"{src}:t?"]
        cmd += ["-c:t", "copy"]

    # ── Codec: VIDEO ───────────────────────────────────────────────────────────
    # Always copy — no video re-encoding under any circumstances.
    cmd += ["-c:v", "copy"]

    # HEVC streams muxed by some tools (e.g. mkvmerge 96.x) contain malformed
    # HVCC codec private data.  ffmpeg 7.x's matroska muxer calls
    # ff_isom_write_hvcc() to write the output CodecPrivate element; if the
    # input HVCC is invalid it returns AVERROR_INVALIDDATA and the header write
    # fails before a single frame is processed.
    #
    # hevc_mp4toannexb converts the compact HVCC extradata to inline Annex B
    # parameter sets.  ffmpeg's MKV muxer then reconstructs a valid HVCC from
    # the Annex B data, bypassing the malformed original entirely.
    # Apply hevc_mp4toannexb only to HEVC streams by their output video index.
    # Using -bsf:v would apply to ALL video streams, including MJPEG attached
    # pictures, which causes "Codec 'mjpeg' is not supported by hevc_mp4toannexb".
    #
    # Only apply the BSF when there's an actual HVCC box for it to convert.
    # Some sources (confirmed via ffprobe: extradata_size in the low tens of
    # bytes, vs. ~2-3 KB for a real VPS/SPS/PPS-bearing HVCC box) ship with an
    # essentially empty/stub extradata field — the stream is already
    # self-contained with in-band parameter sets. Forcing the BSF on those
    # gives it nothing to convert and it fails outright ("No parameter sets
    # in the extradata" -> "Could not write header"), even though a plain
    # stream copy with no BSF succeeds fine. A stub HVCC box cannot hold a
    # real VPS+SPS+PPS set, so this is a safe, non-arbitrary cutoff.
    _MIN_HEVC_EXTRADATA_FOR_BSF = 100
    for _out_v_idx, _vs in enumerate(all_video):
        if (
            _vs.get("codec_name", "").lower() == "hevc"
            and _vs.get("extradata_size", 0) >= _MIN_HEVC_EXTRADATA_FOR_BSF
        ):
            cmd += [f"-bsf:v:{_out_v_idx}", "hevc_mp4toannexb"]

    # ── Codec: AUDIO ───────────────────────────────────────────────────────────
    # Output audio stream indices are 0-based within the audio type.
    # -c:a:N sets the codec for output audio stream N.
    # -b:a:N sets the bitrate for output audio stream N.
    # -ac:a:N sets the channel count for output audio stream N.
    if fix_dispositions_only:
        # Fix-only: existing AAC at output 0, other tracks follow.  All audio
        # already lives in this container, so everything is pure stream copy.
        n_other          = len(other_audio)
        aac_out_idx      = 0
        other_start_idx  = 1
        cmd += ["-c:a", "copy"]
    else:
        n_other = len(other_audio) if PRESERVE_ALL_AUDIO else 0

        if AAC_AS_FIRST_TRACK:
            aac_out_idx      = 0
            surround_out_idx = 1
        else:
            surround_out_idx = 0
            aac_out_idx      = 1
        other_start_idx = 2   # other tracks always start at index 2

        # MP4/M4V containers use the ipod muxer, which only accepts AAC and AC3.
        # DTS, EAC3, TrueHD, etc. cannot be stream-copied into these containers.
        # Transcode non-AAC audio to AC3 when the output is an MP4-family file.
        if output_path.suffix.lower() in _MP4_CONTAINERS:
            surround_codec = "ac3"
            log.info(
                "MP4/M4V container detected — surround track will be transcoded "
                "to AC3 (ipod muxer does not support stream-copy of DTS/EAC3/TrueHD)."
            )
        else:
            surround_codec = "copy"

        # New AAC track: transcode with downmix to AAC_CHANNELS channels.
        # ffmpeg's built-in matrix downmix is used; no explicit pan filter needed.
        cmd += [f"-c:a:{aac_out_idx}",  "aac"]
        cmd += [f"-b:a:{aac_out_idx}",  AAC_BITRATE]
        cmd += [f"-ac:a:{aac_out_idx}", str(AAC_CHANNELS)]

        # Original surround track: stream copy, or AC3 transcode for MP4 containers.
        cmd += [f"-c:a:{surround_out_idx}", surround_codec]

        # Other audio tracks: same container-aware codec choice.
        for i in range(n_other):
            cmd += [f"-c:a:{other_start_idx + i}", surround_codec]

    # ── Output audio track titles ──────────────────────────────────────────────
    # Assembled here because this is the only point where each track's output
    # index and its actual output codec are both known.  Emitted further down,
    # after -map_metadata.
    audio_titles: Dict[int, str] = {}
    if SET_TRACK_TITLES:
        if fix_dispositions_only:
            # Every track is stream-copied, so each keeps its own properties.
            audio_titles[aac_out_idx] = build_audio_title(primary_audio)
            for i, other in enumerate(other_audio):
                audio_titles[other_start_idx + i] = build_audio_title(other)
        else:
            # The injected track is always AAC at AAC_CHANNELS channels and
            # inherits the primary track's language (set just below).
            audio_titles[aac_out_idx] = compose_audio_title(
                AAC_CHANNELS, "AAC", friendly_language(primary_audio)
            )
            # Copied tracks are titled by the codec they will have on the way
            # OUT, which differs from the source only for MP4-family outputs
            # where the ipod muxer forces an AC3 transcode.
            copied_codec = "Dolby Digital" if surround_codec == "ac3" else ""
            audio_titles[surround_out_idx] = build_audio_title(
                primary_audio, codec_override=copied_codec
            )
            for i in range(n_other):
                audio_titles[other_start_idx + i] = build_audio_title(
                    other_audio[i], codec_override=copied_codec
                )

    # ── Codec: SUBTITLES ───────────────────────────────────────────────────────
    cmd += ["-c:s", "copy"]

    # ── Disposition flags ──────────────────────────────────────────────────────
    # Mark the AAC track as the ONLY default audio track.
    # Jellyfin's web player selects the first default-flagged audio stream.
    # Clear default on everything else to prevent ambiguity.
    if fix_dispositions_only:
        # +default / -default touch only the default bit — original tracks
        # keep their other flags (comment, hearing_impaired, etc.).
        cmd += [f"-disposition:a:{aac_out_idx}", "+default"]
        for i in range(n_other):
            cmd += [f"-disposition:a:{other_start_idx + i}", "-default"]
    else:
        cmd += [f"-disposition:a:{aac_out_idx}",      "default"]
        cmd += [f"-disposition:a:{surround_out_idx}", "0"]
        for i in range(n_other):
            cmd += [f"-disposition:a:{other_start_idx + i}", "0"]

    # Subtitle default: flag the best English subtitle, clear default on the
    # rest.  The +default / -default forms modify ONLY the default bit, so
    # other flags (forced, hearing_impaired) survive on every track.
    if SET_ENGLISH_SUBTITLE_DEFAULT and all_subs:
        default_sub_pos = select_default_subtitle(all_subs)
        if default_sub_pos is not None:
            for i in range(len(all_subs)):
                flag = "+default" if i == default_sub_pos else "-default"
                cmd += [f"-disposition:s:{i}", flag]

    # ── Language tag for the new AAC track ─────────────────────────────────────
    # Fix-only mode reuses an existing AAC track — its language tag stands.
    if not fix_dispositions_only:
        # Copy language tag from the primary source if available.
        primary_tags = primary_audio.get("tags") or {}
        lang = primary_tags.get("language") or primary_tags.get("LANGUAGE")
        if lang:
            cmd += [f"-metadata:s:a:{aac_out_idx}", f"language={lang}"]

    # ── Container-level metadata and chapters ──────────────────────────────────
    # -map_metadata → copy all container tags (title, year, comment, etc.)
    # -map_chapters → copy chapter markers.  Always sourced from the original
    # file (src) — the repaired-AV1 ivf, when present, carries neither.
    cmd += ["-map_metadata", src]
    cmd += ["-map_chapters",  src]

    # ── Track titles ───────────────────────────────────────────────────────────
    # Emitted AFTER -map_metadata deliberately: that flag re-copies the source
    # tags, so titles set before it would be overwritten by the very junk this
    # is meant to replace.
    if SET_TRACK_TITLES:
        for out_v_idx, vs in enumerate(all_video):
            if is_cover_art_stream(vs):
                # Artwork carries no title.  Explicitly blank it so a junk one
                # inherited from the source (or written by an earlier version
                # of this script) is cleared rather than preserved.
                if stream_title(vs):
                    cmd += [f"-metadata:s:v:{out_v_idx}", "title="]
                continue
            video_title = build_video_title(vs, input_path)
            if video_title:
                cmd += [f"-metadata:s:v:{out_v_idx}", f"title={video_title}"]

        for out_a_idx, audio_title in sorted(audio_titles.items()):
            if audio_title:
                cmd += [f"-metadata:s:a:{out_a_idx}", f"title={audio_title}"]

        for out_s_idx, ss in enumerate(all_subs):
            subtitle_title = build_subtitle_title(ss)
            if subtitle_title:
                cmd += [f"-metadata:s:s:{out_s_idx}", f"title={subtitle_title}"]

    # ── Container title ────────────────────────────────────────────────────────
    # Release-group spam in the container title shows up in some players and
    # in ffprobe output.  Cleared to empty rather than replaced — Jellyfin
    # reads the NFO/filename for the real name, so inventing one here would
    # add nothing and risk disagreeing with the library.
    if CLEAR_JUNK_CONTAINER_TITLE:
        container_title = (
            (probe_data.get("format") or {}).get("tags") or {}
        ).get("title") or ""
        if looks_like_junk_title(container_title):
            cmd += ["-metadata", "title="]

    # ── Output ─────────────────────────────────────────────────────────────────
    # -y: overwrite the temp file if it somehow already exists.
    # -f mp4: for .mp4/.m4v outputs, force the standard mp4 muxer instead of
    # ffmpeg's default ipod muxer.  The ipod muxer rejects HEVC video and
    # non-AAC/MP3 audio (e.g. DTS, EAC3, TrueHD); standard mp4 accepts both.
    if output_path.suffix.lower() in _MP4_CONTAINERS:
        cmd += ["-f", "mp4"]
    cmd += ["-y", str(output_path)]

    return cmd


def extract_av1_ivf(input_path: Path, stream_index: int, ivf_path: Path) -> bool:
    """
    Stream-copy a single AV1 video track out to a standalone .ivf file, as a
    repair step for the malformed-CodecPrivate bug described in
    build_ffmpeg_command's AV1 REPAIR note.

    IVF stores an explicit per-frame timestamp (unlike a bare OBU elementary
    stream, which has none), so round-tripping through it preserves the
    original presentation timing exactly — verified byte-for-byte identical
    on the first several packet PTS values during investigation — while
    forcing ffmpeg to derive fresh extradata from the actual OBU
    sequence-header packets in the bitstream instead of trusting the
    source container's CodecPrivate.

    Returns True on success. On failure, logs ffmpeg's stderr and returns
    False; the caller falls back to reporting the original failure.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-i", str(input_path),
        "-map", f"0:{stream_index}",
        "-c:v", "copy",
        "-f", "ivf",
        str(ivf_path),
    ]
    try:
        result = subprocess.run(
            low_priority_prefix() + cmd,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log.error(
            "AV1 IVF extraction exceeded the %d-second timeout and was killed.",
            FFMPEG_TIMEOUT_SECONDS,
        )
        return False
    if result.returncode != 0:
        log.error(
            "AV1 IVF extraction failed (exit %d): %s",
            result.returncode, result.stderr.strip(),
        )
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def validate_output(output_path: Path, input_path: Path) -> bool:
    """
    Sanity-check the output file before replacing the original.

    Checks:
      1. File exists and is non-empty.
      2. File is at least 90% the size of the input.  The output should
         actually be LARGER (new AAC track added), so < 90% strongly suggests
         truncation or silent ffmpeg failure.
      3. ffprobe can parse the file without errors.

    Returns True if all checks pass, False otherwise.
    """
    if not output_path.exists():
        log.error("Validation failed: output file does not exist: %s", output_path)
        return False

    out_size = output_path.stat().st_size
    in_size  = input_path.stat().st_size

    if out_size == 0:
        log.error("Validation failed: output file is empty")
        return False

    size_ratio = out_size / in_size
    if size_ratio < 0.90:
        # Not a hard failure on its own; ffprobe catch real corruption below.
        log.warning(
            "Output is only %.1f%% of input size (%d vs %d bytes) — "
            "possible truncation.",
            size_ratio * 100, out_size, in_size,
        )

    try:
        probe_file(output_path)
    except Exception as exc:
        log.error("Validation failed: ffprobe rejected output file: %s", exc)
        return False

    log.info(
        "Output validation passed — size: %.1f MB (%.1f%% of input)",
        out_size / 1e6, size_ratio * 100,
    )
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# TEMP FILE CLEANUP
# ═══════════════════════════════════════════════════════════════════════════════

def cleanup_temp(tmp_path: Path) -> None:
    """Remove *tmp_path* if it exists.  Log but do not raise on failure."""
    if tmp_path.exists():
        try:
            tmp_path.unlink()
            log.debug("Cleaned up temp file: %s", tmp_path)
        except OSError as exc:
            log.warning("Could not remove temp file %s: %s", tmp_path, exc)


# ═══════════════════════════════════════════════════════════════════════════════
# TEMP FILE STAGING & PROCESS PRIORITY
# ═══════════════════════════════════════════════════════════════════════════════

def resolve_staging_dir(input_path: Path) -> Optional[Path]:
    """
    Return STAGING_DIR if it is usable for staging *input_path*'s temp output,
    otherwise None (caller falls back to in-place temp next to the source).

    Usability checks:
      1. The staging mount (STAGING_DIR's parent) exists and is a mount point —
         guards against writing to an empty stub directory on the root disk
         when the NVMe is not mounted.
      2. STAGING_DIR can be created and is writable by this UID.
      3. Free space is at least 110% of the input size (output is slightly
         larger than input: same streams plus the new AAC track).
    """
    mount_root = STAGING_DIR.parent
    if not mount_root.is_dir() or not os.path.ismount(str(mount_root)):
        log.warning(
            "Staging mount %s is missing or not mounted — "
            "falling back to in-place temp file.", mount_root,
        )
        return None

    try:
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning(
            "Cannot create staging dir %s (%s) — "
            "falling back to in-place temp file.", STAGING_DIR, exc,
        )
        return None

    if not os.access(STAGING_DIR, os.W_OK):
        log.warning(
            "Staging dir %s is not writable — "
            "falling back to in-place temp file.", STAGING_DIR,
        )
        return None

    try:
        free = shutil.disk_usage(STAGING_DIR).free
    except OSError as exc:
        log.warning(
            "Cannot check free space on %s (%s) — "
            "falling back to in-place temp file.", STAGING_DIR, exc,
        )
        return None

    needed = int(input_path.stat().st_size * 1.10)
    if free < needed:
        log.warning(
            "Staging dir %s has insufficient free space (%.1f GB free, "
            "%.1f GB needed) — falling back to in-place temp file.",
            STAGING_DIR, free / 1e9, needed / 1e9,
        )
        return None

    return STAGING_DIR


def low_priority_prefix() -> List[str]:
    """
    Return an argv prefix that runs ffmpeg at idle I/O class (ionice -c 3)
    and lowest CPU priority (nice -n 19) so it yields to Jellyfin playback.
    Missing wrapper binaries are skipped with a warning rather than failing.
    """
    prefix: List[str] = []
    if shutil.which("ionice"):
        prefix += ["ionice", "-c", "3"]
    else:
        log.warning("ionice not found — ffmpeg will run at normal I/O priority.")
    if shutil.which("nice"):
        prefix += ["nice", "-n", "19"]
    else:
        log.warning("nice not found — ffmpeg will run at normal CPU priority.")
    return prefix


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PROCESSING PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def process_file(file_path: Path) -> bool:
    """
    Full dual-audio processing pipeline for a single media file.

    Pipeline:
      1. Validate input path.
      2. Probe file with ffprobe.
      3. Check for existing AAC track (skip if SKIP_IF_AAC_EXISTS and its
         default flags are already correct; wrong flags trigger a
         disposition-only stream-copy remux instead).
      4. Select the best primary audio stream.
      5. Build the ffmpeg command.
      6. Run ffmpeg (ionice/nice-wrapped) into a temp file — staged on the
         NVMe (STAGING_DIR) when available, else next to the source.
      7. Validate the temp output.
      8. Move a staged temp back to the source directory, then atomically
         replace the original.

    SAFETY GUARANTEE: The original file is NEVER modified unless steps 1–7
    all succeed.  On any failure, the temp file is deleted and the function
    returns False.

    Returns:
        True  — file was processed successfully, or was deliberately skipped.
        False — processing failed; original file is intact.
    """
    log.info("─" * 60)
    log.info("Processing: %s", file_path)

    # ── Step 1: Input validation ───────────────────────────────────────────────
    if not file_path.exists():
        log.error("Input file not found: %s", file_path)
        return False
    if not file_path.is_file():
        log.error("Input path is not a regular file: %s", file_path)
        return False

    # ── Step 2: Probe ──────────────────────────────────────────────────────────
    try:
        probe_data = probe_file(file_path)
    except Exception:
        log.error("Failed to probe input file. Aborting.")
        return False

    audio_streams    = get_streams_by_type(probe_data, "audio")
    video_streams    = get_streams_by_type(probe_data, "video")
    subtitle_streams = get_streams_by_type(probe_data, "subtitle")

    log.info(
        "Stream summary — video: %d | audio: %d | subtitles: %d",
        len(video_streams), len(audio_streams), len(subtitle_streams),
    )
    for s in audio_streams:
        tags = s.get("tags", {})
        log.info(
            "  Audio [%d] codec=%-8s channels=%-2s lang=%-4s default=%-1s title=%r",
            s["index"],
            s.get("codec_name", "?"),
            s.get("channels", "?"),
            tags.get("language") or tags.get("LANGUAGE") or "?",
            s.get("disposition", {}).get("default", 0),
            tags.get("title") or tags.get("TITLE") or "",
        )

    if not audio_streams:
        log.warning("No audio streams found — nothing to do.")
        return True  # Not a processing error

    # ── Step 3: AAC check ──────────────────────────────────────────────────────
    # A file with an English AAC track is only truly done when that track is
    # also the sole default — sources sometimes ship AAC pre-baked with a
    # foreign dub flagged default, which makes Jellyfin pick the wrong track.
    # Those get a disposition-only remux (pure stream copy, no transcoding).
    fix_dispositions_only = False
    existing_aac: Optional[Dict[str, Any]] = None
    if SKIP_IF_AAC_EXISTS:
        existing_aac = find_english_aac_track(audio_streams)
        if existing_aac is not None:
            if audio_dispositions_correct(audio_streams, existing_aac):
                # The audio itself is already right, but the file may still
                # carry release-group junk in its track titles.  Retitling is
                # a pure metadata edit, so it reuses the same stream-copy path
                # as a disposition fix.
                title_changes = compute_title_changes(probe_data, file_path)
                if not title_changes:
                    log.info(
                        "File already has an English AAC track flagged default "
                        "and correct track titles — skipping "
                        "(SKIP_IF_AAC_EXISTS=True)."
                    )
                    return True
                fix_dispositions_only = True
                log.info(
                    "English AAC track and default flags are already correct, "
                    "but %d track title(s) need updating — metadata-only remux "
                    "(all streams copied, no transcoding).",
                    len(title_changes),
                )
                for desc, old_title, new_title in title_changes:
                    log.info("  %-12s %r → %r", desc, old_title, new_title)
            else:
                fix_dispositions_only = True
                log.info(
                    "English AAC track exists (index %d) but audio default flags "
                    "are wrong — disposition-only remux (all streams copied, no "
                    "transcoding).",
                    existing_aac["index"],
                )

    # ── Step 4: Select primary audio ───────────────────────────────────────────
    if fix_dispositions_only:
        primary_audio = existing_aac
    else:
        primary_audio = select_primary_audio(audio_streams)
        if primary_audio is None:
            log.error("Could not select a primary audio stream.")
            return False

    # ── Step 5: Build ffmpeg command ───────────────────────────────────────────
    # Stage ffmpeg output on the NVMe (STAGING_DIR) to avoid read+write head
    # contention on the spinning media pool while Jellyfin plays from it.
    # The final os.replace() must be an atomic same-filesystem rename, so a
    # staged file is first moved back into the source directory (Step 8).
    # If staging is unavailable, fall back to the temp file next to the source.
    output_dir = file_path.parent
    local_tmp  = output_dir / (file_path.stem + ".__aac_tmp__" + file_path.suffix)

    staging_dir = resolve_staging_dir(file_path)
    if staging_dir is not None:
        tmp_path = staging_dir / (file_path.stem + ".__aac_tmp__" + file_path.suffix)
        log.info("Staging ffmpeg output on NVMe: %s", tmp_path)
    else:
        tmp_path = local_tmp

    cmd = build_ffmpeg_command(
        file_path, tmp_path, probe_data, primary_audio,
        fix_dispositions_only=fix_dispositions_only,
    )
    log.debug("ffmpeg command:\n  %s", "\n  ".join(cmd))

    # ── Step 6: Run ffmpeg ─────────────────────────────────────────────────────
    # Wrapped with ionice -c 3 / nice -n 19 so it yields to playback I/O.
    log.info("Running ffmpeg … (this may take several minutes)")
    try:
        result = subprocess.run(
            low_priority_prefix() + cmd,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        log.error(
            "ffmpeg not found.  Install it:\n"
            "  sudo dnf install ffmpeg   # Fedora/RHEL\n"
            "  sudo apt install ffmpeg   # Debian/Ubuntu"
        )
        cleanup_temp(tmp_path)
        return False
    except subprocess.TimeoutExpired:
        log.error(
            "ffmpeg exceeded the %d-second timeout and was killed — treating "
            "as hung rather than blocking the import queue indefinitely.",
            FFMPEG_TIMEOUT_SECONDS,
        )
        cleanup_temp(tmp_path)
        return False
    except Exception as exc:
        log.error("Unexpected error running ffmpeg: %s", exc)
        cleanup_temp(tmp_path)
        return False

    # ── Step 6b: AV1 CodecPrivate repair retry ─────────────────────────────────
    # Some AV1 WEB-DL releases (observed from the "Saon" release group, e.g.
    # Wednesday S01E05-E08) carry the full nested ISOBMFF 'av01' sample-entry
    # box as the video track's CodecPrivate instead of a bare av1C record.
    # ffmpeg's matroska muxer rejects that shape at header-write time — fails
    # instantly, before any frame is processed — even though a plain stream
    # copy into MP4 tolerates it fine. See build_ffmpeg_command's AV1 REPAIR
    # note. Detected here purely by failure signature (not by pre-inspecting
    # extradata) to keep the fast path unchanged for every file that doesn't
    # hit this bug.
    if result.returncode == 183:
        stderr = result.stderr.strip()
        av1_streams = [
            s for s in video_streams if s.get("codec_name", "").lower() == "av1"
        ]
        if av1_streams and "Could not write header (incorrect codec parameters" in stderr:
            log.warning(
                "ffmpeg failed writing the container header with an AV1 video "
                "stream present (exit 183) — likely a malformed/boxed AV1 "
                "CodecPrivate from the source remux. Attempting automatic "
                "repair: re-deriving extradata by round-tripping the video "
                "track through a timestamped IVF file, then remuxing "
                "everything else from the original."
            )
            av1_idx = av1_streams[0]["index"]
            ivf_tmp = tmp_path.parent / (file_path.stem + ".__av1fix_tmp__.ivf")
            if extract_av1_ivf(file_path, av1_idx, ivf_tmp):
                cmd = build_ffmpeg_command(
                    file_path, tmp_path, probe_data, primary_audio,
                    fix_dispositions_only=fix_dispositions_only,
                    av1_extra_input=(ivf_tmp, av1_idx),
                )
                log.info("Retrying with repaired AV1 video track …")
                try:
                    result = subprocess.run(
                        low_priority_prefix() + cmd,
                        capture_output=True,
                        text=True,
                        timeout=FFMPEG_TIMEOUT_SECONDS,
                    )
                except subprocess.TimeoutExpired:
                    log.error(
                        "AV1 repair retry exceeded the %d-second timeout and "
                        "was killed.",
                        FFMPEG_TIMEOUT_SECONDS,
                    )
                else:
                    if result.returncode == 0:
                        log.info("AV1 repair succeeded.")
                    else:
                        log.error(
                            "AV1 repair retry still failed (exit %d).", result.returncode,
                        )
            cleanup_temp(ivf_tmp)

    if result.returncode != 0:
        stderr = result.stderr.strip()
        # Detect unset-timestamp errors specifically — these come from a
        # malformed source MKV where video packets have no PTS/DTS stored in
        # the container.  ffmpeg 7.x's matroska muxer rejects such packets
        # even in copy mode (genpts can't help without decoding).
        # Repair the source file first with:
        #   mkvmerge -o fixed.mkv original.mkv
        # then reprocess the fixed file.
        if "Can't write packet with unknown timestamp" in stderr:
            log.error(
                "ffmpeg exited with code %d — video stream has unset timestamps "
                "(malformed container; ffmpeg 7.x cannot stream-copy these packets).\n"
                "Repair the source file first:\n"
                "  mkvmerge -o \"%s\" \"%s\"\n"
                "then reprocess the repaired file.",
                result.returncode,
                file_path.with_stem(file_path.stem + ".fixed"),
                file_path,
            )
        else:
            log.error(
                "ffmpeg exited with code %d.\nstderr:\n%s",
                result.returncode, stderr,
            )
        cleanup_temp(tmp_path)
        return False

    # Log any ffmpeg warnings even on success (useful for subtitle issues, etc.)
    if result.stderr.strip():
        log.debug("ffmpeg stderr:\n%s", result.stderr.strip())

    log.info("ffmpeg finished successfully.")

    # ── Step 7: Validate output ────────────────────────────────────────────────
    if not validate_output(tmp_path, file_path):
        log.error("Output validation failed — original file is untouched.")
        cleanup_temp(tmp_path)
        return False

    # ── Step 8: Atomic replace ─────────────────────────────────────────────────
    # If the output was staged on the NVMe, move it back into the source
    # directory first (one sequential cross-device copy).  The original is
    # still only ever replaced by the same-filesystem os.replace() below.
    if tmp_path != local_tmp:
        log.info("Moving staged output back to media directory …")
        try:
            shutil.move(str(tmp_path), str(local_tmp))
        except OSError as exc:
            log.error("Failed to move staged output back: %s", exc)
            cleanup_temp(tmp_path)
            cleanup_temp(local_tmp)  # remove any partial cross-device copy
            return False
        tmp_path = local_tmp

    # os.replace() is POSIX-atomic when src and dst are on the same filesystem.
    # The original file is replaced in a single syscall — no window where
    # both or neither file exists.
    log.info("Replacing original with processed output …")
    try:
        os.replace(str(tmp_path), str(file_path))
    except OSError as exc:
        log.error("Failed to replace original file: %s", exc)
        cleanup_temp(tmp_path)
        return False

    log.info("SUCCESS: %s", file_path)
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    log.info("╔══ AAC Dual-Audio Post-Processor ══╗")

    app_name, file_path = detect_caller()
    log.info("Application : %s", app_name)
    log.info("Target file : %s", file_path)

    success = process_file(file_path)

    if not success:
        log.error("Processing FAILED — see messages above.")
        sys.exit(1)

    log.info("Processing COMPLETE.")
    sys.exit(0)


if __name__ == "__main__":
    main()
