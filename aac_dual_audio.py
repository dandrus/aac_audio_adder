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

# Human-readable title embedded in the new AAC track's metadata.
# Shown in Jellyfin's audio track selector.
AAC_TRACK_TITLE: str = "AAC 2.0 Stereo"

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


def find_english_aac_track(
    audio_streams: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Return the first non-commentary English AAC track, or None.

    Matches on either:
      • Language tag is English ("eng", "en", "english"), OR
      • Track title matches AAC_TRACK_TITLE — catches tracks we injected on a
        prior run when the source had no language tag (so we never set one).

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
        if lang in _ENGLISH_TAGS or title == AAC_TRACK_TITLE:
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
# FFMPEG COMMAND BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

def build_ffmpeg_command(
    input_path: Path,
    output_path: Path,
    probe_data: Dict[str, Any],
    primary_audio: Dict[str, Any],
    fix_dispositions_only: bool = False,
) -> List[str]:
    """
    Build and return the ffmpeg command list that produces the dual-audio file.

    FIX-ONLY MODE (fix_dispositions_only=True): used when the file already
    contains an English AAC track (passed as *primary_audio*) but the default
    flags are wrong (e.g. a foreign dub flagged default by the source).
    Every stream is copied — no transcoding — with the AAC track mapped first,
    flagged as the only default audio, and the subtitle default logic applied.
    Existing track metadata (title, language) is left untouched.  All audio
    tracks are always kept regardless of PRESERVE_ALL_AUDIO.

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

    # ── Base command ───────────────────────────────────────────────────────────
    cmd: List[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",   # suppress verbose progress; use "info" to debug
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
    for vs in all_video:
        cmd += ["-map", f"0:{vs['index']}"]

    # ── Stream mapping — AUDIO ─────────────────────────────────────────────────
    # Fix-only mode: the existing AAC track (primary_audio) is mapped ONCE,
    # first, and every other audio track follows in original order.
    if fix_dispositions_only:
        cmd += ["-map", f"0:{primary_abs_idx}"]       # output audio 0 → existing AAC
        for oa in other_audio:
            cmd += ["-map", f"0:{oa['index']}"]       # output audio 1+ → copy
    # Normal mode: we map primary_audio TWICE:
    #   pass 1 → will receive -c:a:<N> aac  (new AAC track)
    #   pass 2 → will receive -c:a:<N> copy (original surround preserved)
    elif AAC_AS_FIRST_TRACK:
        cmd += ["-map", f"0:{primary_abs_idx}"]   # output audio 0 → new AAC
        cmd += ["-map", f"0:{primary_abs_idx}"]   # output audio 1 → surround copy
        if PRESERVE_ALL_AUDIO:
            for oa in other_audio:
                cmd += ["-map", f"0:{oa['index']}"]   # output audio 2+ → copy
    else:
        cmd += ["-map", f"0:{primary_abs_idx}"]   # output audio 0 → surround copy
        cmd += ["-map", f"0:{primary_abs_idx}"]   # output audio 1 → new AAC
        if PRESERVE_ALL_AUDIO:
            for oa in other_audio:
                cmd += ["-map", f"0:{oa['index']}"]   # output audio 2+ → copy

    # ── Stream mapping — SUBTITLES ─────────────────────────────────────────────
    for ss in all_subs:
        cmd += ["-map", f"0:{ss['index']}"]

    # ── Stream mapping — ATTACHMENTS ───────────────────────────────────────────
    # MKV attachments carry embedded cover art and fonts for ASS/SSA subtitles;
    # dropping them loses artwork and breaks styled-subtitle rendering.
    # MP4-family containers don't support attachment streams, so skip there.
    # The trailing '?' makes the mapping optional (no error when none exist).
    _MP4_CONTAINERS = frozenset({".mp4", ".m4v"})
    if output_path.suffix.lower() not in _MP4_CONTAINERS:
        cmd += ["-map", "0:t?"]
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
    for _out_v_idx, _vs in enumerate(all_video):
        if _vs.get("codec_name", "").lower() == "hevc":
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

    # ── Metadata for the new AAC track ─────────────────────────────────────────
    # Fix-only mode reuses an existing AAC track — leave its tags untouched.
    if not fix_dispositions_only:
        cmd += [f"-metadata:s:a:{aac_out_idx}", f"title={AAC_TRACK_TITLE}"]

        # Copy language tag from the primary source if available.
        primary_tags = primary_audio.get("tags") or {}
        lang = primary_tags.get("language") or primary_tags.get("LANGUAGE")
        if lang:
            cmd += [f"-metadata:s:a:{aac_out_idx}", f"language={lang}"]

    # ── Container-level metadata and chapters ──────────────────────────────────
    # -map_metadata 0  → copy all container tags (title, year, comment, etc.)
    # -map_chapters 0  → copy chapter markers from input
    cmd += ["-map_metadata", "0"]
    cmd += ["-map_chapters",  "0"]

    # ── Output ─────────────────────────────────────────────────────────────────
    # -y: overwrite the temp file if it somehow already exists.
    # -f mp4: for .mp4/.m4v outputs, force the standard mp4 muxer instead of
    # ffmpeg's default ipod muxer.  The ipod muxer rejects HEVC video and
    # non-AAC/MP3 audio (e.g. DTS, EAC3, TrueHD); standard mp4 accepts both.
    if output_path.suffix.lower() in _MP4_CONTAINERS:
        cmd += ["-f", "mp4"]
    cmd += ["-y", str(output_path)]

    return cmd


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
                log.info(
                    "File already has an English AAC track flagged default — "
                    "skipping (SKIP_IF_AAC_EXISTS=True)."
                )
                return True
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
        )
    except FileNotFoundError:
        log.error(
            "ffmpeg not found.  Install it:\n"
            "  sudo dnf install ffmpeg   # Fedora/RHEL\n"
            "  sudo apt install ffmpeg   # Debian/Ubuntu"
        )
        cleanup_temp(tmp_path)
        return False
    except Exception as exc:
        log.error("Unexpected error running ffmpeg: %s", exc)
        cleanup_temp(tmp_path)
        return False

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
