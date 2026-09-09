# aac_audio_adder

Bakes a browser-friendly AAC stereo track into MKV/MP4 files alongside the original surround audio. Solves Jellyfin web clients being forced into HLS transcoding when the source audio is EAC3/Atmos/DTS — formats that browsers cannot natively decode.

With an AAC track marked as default, browsers direct-play the file. Clients that support surround (Apple TV app, Jellyfin Android, etc.) can still select the original track.

## Scripts

| Script | Purpose |
|---|---|
| `aac_dual_audio.py` | Radarr/Sonarr post-processing hook — runs automatically on import |
| `aac_library_batch.py` | One-shot batch processor for an existing library |

## Requirements

- Python 3.8+
- `ffmpeg` and `ffprobe`

```bash
# Fedora/RHEL
sudo dnf install ffmpeg

# Debian/Ubuntu
sudo apt install ffmpeg
```

## Setup: Radarr / Sonarr

1. Copy `aac_dual_audio.py` to a location accessible by your Radarr/Sonarr container or process.
2. In Radarr/Sonarr → **Settings → Connect**, add a **Custom Script** connection:
   - **Path**: `/path/to/aac_dual_audio.py`
   - **On Import**: enabled
   - **On Upgrade**: enabled (recommended)
3. Click **Test** to verify the connection — the script will log `Test event received — configuration OK.` and exit cleanly.

## Usage: Batch Processor

```bash
# Dry run — shows what would be processed without changing anything
python3 aac_library_batch.py /media/jellyfin --dry-run

# Process a full library
python3 aac_library_batch.py /media/jellyfin

# 2 parallel workers (only if you have spare I/O bandwidth)
python3 aac_library_batch.py /media/jellyfin --workers 2

# Process only MKV files, stop after 5 files (useful for testing)
python3 aac_library_batch.py /media/jellyfin --ext mkv --limit 5

# Resume an interrupted run
python3 aac_library_batch.py /media/jellyfin \
  --resume-log /var/log/aac_done.log \
  --log-file /var/log/aac_batch.log
```

Both scripts must be in the same directory — `aac_library_batch.py` imports shared logic from `aac_dual_audio.py`.

## Configuration

All settings are constants at the top of `aac_dual_audio.py`:

| Constant | Default | Description |
|---|---|---|
| `AAC_BITRATE` | `"384k"` | Bitrate for the new AAC track |
| `AAC_CHANNELS` | `2` | Output channels (2 = stereo) |
| `SET_TRACK_TITLES` | `True` | Rewrite track titles to a consistent scheme (see below) |
| `CLEAR_JUNK_CONTAINER_TITLE` | `True` | Blank the container title when it is release-group spam |
| `SKIP_IF_AAC_EXISTS` | `True` | Skip files that already have an AAC track |
| `PRESERVE_ALL_AUDIO` | `True` | Keep all original audio tracks (dubs, commentary, etc.) |
| `AAC_AS_FIRST_TRACK` | `True` | Put AAC at track index 0 and mark it default |

### Track titles

With `SET_TRACK_TITLES` enabled, every track is retitled from its own
properties, replacing release-group spam like `..:::EmpireBestTV.Com:::.` or
`ExtraFlix.Pw | English AAC2.0 @ 128 kbps`:

| Type | Scheme | Example |
|---|---|---|
| video | `<resolution> <source> <codec>` | `720p WEBDL HEVC` |
| audio | `<layout> <codec> <language>` | `5.1 Dolby Digital Plus English` |
| subtitle | `<language> (<qualifiers>)` | `Persian (Forced)`, `English (SDH)` |

Commentary, forced, and SDH tracks keep that distinction as a parenthetical
qualifier, so a commentary track is never flattened into plain dialogue. A file
that already has a correct AAC track but junk titles gets a metadata-only
remux — a pure stream copy, no re-encoding, usually a few seconds.

## How It Works

1. **Probe** — `ffprobe` reads all stream metadata as JSON.
2. **Skip check** — if an AAC track already exists, is flagged default, and all track titles are already correct, exit. If only the titles or default flags are wrong, a metadata-only remux runs instead (stream copy, no transcoding).
3. **Select primary audio** — filters out commentary tracks, prefers English, prefers the encoder-default-flagged track, then ranks by codec quality (TrueHD / DTS-HD MA > DTS > EAC3 > AC3 …).
4. **Build ffmpeg command** — maps the primary stream *twice*: once transcoded to AAC, once stream-copied as the original surround.
5. **Write to temp file** — output goes to `filename.__aac_tmp__.ext` in the same directory.
6. **Validate** — checks file size (≥ 90% of input) and re-probes with ffprobe.
7. **Atomic replace** — `os.replace()` swaps the temp file over the original in a single syscall. The original is never touched until this step.

## Safety

- The original file is **never modified** unless all validation steps pass.
- A failed transcode leaves the original completely intact.
- `os.replace()` is atomic on POSIX — no window where the file is missing or partially written.

## License

[CC BY-NC 4.0](LICENSE) — free to use and adapt for non-commercial purposes with attribution.
