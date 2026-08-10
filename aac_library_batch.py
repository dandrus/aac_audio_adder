#!/usr/bin/env python3
"""
aac_library_batch.py
─────────────────────────────────────────────────────────────────────────────
Batch processor that walks an existing Jellyfin media library and adds a
browser-friendly AAC stereo track to every file that lacks one.

Uses the same audio-selection and stream-handling logic as aac_dual_audio.py.
Both scripts must live in the same directory (this script imports from it).

USAGE
─────
  # Dry run — shows what would be processed without changing anything
  python3 aac_library_batch.py /media/jellyfin --dry-run

  # Process a library (sequential, safe default)
  python3 aac_library_batch.py /media/jellyfin

  # Process with 2 parallel workers (only if you have spare I/O bandwidth)
  python3 aac_library_batch.py /media/jellyfin --workers 2

  # Process only MKV files in a specific show directory
  python3 aac_library_batch.py "/media/jellyfin/TV/Breaking Bad" --ext mkv

  # Resume a previous interrupted run (skips files listed in resume log)
  python3 aac_library_batch.py /media/jellyfin --resume-log /var/log/aac_done.log

  # Write all results to a log file
  python3 aac_library_batch.py /media/jellyfin --log-file /var/log/aac_batch.log

SAFETY NOTES
────────────
  • The original file is never modified unless ffmpeg succeeds and validation
    passes.  Failed files are left exactly as they were.
  • Run during off-peak hours — ffmpeg is CPU-intensive and performs heavy
    sequential disk I/O.  Consider pausing Jellyfin scans while running.
  • With --workers > 1, each worker reads and writes a different file
    simultaneously.  Only use if your storage can handle concurrent I/O.
  • A Ctrl-C during processing cleans up the current temp file and exits
    gracefully.  Already-completed files are not re-processed on the next run
    (they now have an AAC track and will be skipped by SKIP_IF_AAC_EXISTS).
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# Import shared logic from aac_dual_audio.py
# Both scripts must be in the same directory (or on PYTHONPATH).
# ─────────────────────────────────────────────────────────────────────────────
try:
    from aac_dual_audio import (
        probe_file,
        get_streams_by_type,
        find_english_aac_track,
        audio_dispositions_correct,
        process_file,
        SKIP_IF_AAC_EXISTS,
    )
except ImportError as _err:
    print(
        f"ERROR: Cannot import from aac_dual_audio.py: {_err}\n"
        "Both scripts must be in the same directory.\n"
        "Run from that directory, or add it to PYTHONPATH.",
        file=sys.stderr,
    )
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

# File extensions to scan.  Add/remove as needed.
# MKV is the primary target; others included for completeness.
# NOTE: MP4 containers do not support all subtitle formats (ASS/SSA/SRT).
# If your library has MP4 files with subtitles, review the warnings output
# by ffmpeg carefully.
DEFAULT_MEDIA_EXTENSIONS: Tuple[str, ...] = (".mkv", ".mp4", ".ts", ".m4v")

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

def setup_logging(log_file: Optional[str], verbose: bool) -> logging.Logger:
    """Configure root logger with stdout handler and optional file handler."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt   = "%(asctime)s [%(levelname)s] %(message)s"
    date  = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt, date)

    # logging.basicConfig() is a no-op when the root logger already has handlers
    # (aac_dual_audio.py configures it at import time), so configure directly.
    root = logging.getLogger()
    root.setLevel(level)
    for h in root.handlers:
        h.setFormatter(formatter)
        h.setLevel(level)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(formatter)
        fh.setLevel(level)
        root.addHandler(fh)

    return logging.getLogger(__name__)


# Module-level logger; replaced in main() after argument parsing.
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# FILE DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

def find_media_files(
    root: Path,
    extensions: Tuple[str, ...],
) -> List[Path]:
    """
    Recursively walk *root* and return a sorted list of media files whose
    extension (case-insensitive) is in *extensions*.

    Sorted by file path so processing order is deterministic and easy to
    follow in logs.

    A directory os.walk cannot read (permission denied, stale mount, etc.)
    is logged and skipped rather than silently dropped from the results.
    """
    def _on_walk_error(err: OSError) -> None:
        log.warning("Cannot read directory %s (%s) — skipping.", err.filename, err)

    found: List[Path] = []
    for dirpath, _dirs, filenames in os.walk(root, onerror=_on_walk_error):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if fpath.suffix.lower() in extensions:
                found.append(fpath)
    return sorted(found)


# ═══════════════════════════════════════════════════════════════════════════════
# RESUME LOG
# ═══════════════════════════════════════════════════════════════════════════════

def load_resume_log(resume_log_path: Optional[str]) -> set:
    """
    Load a set of already-processed absolute file paths from a plain-text
    resume log (one path per line, lines starting with '#' are comments).

    Returns an empty set if resume_log_path is None or the file doesn't exist.
    """
    if not resume_log_path:
        return set()
    p = Path(resume_log_path)
    if not p.exists():
        return set()
    paths: set = set()
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                paths.add(line)
    log.info("Resume log loaded — %d already-processed paths", len(paths))
    return paths


def append_to_resume_log(resume_log_path: Optional[str], file_path: Path) -> None:
    """Append a successfully processed file path to the resume log."""
    if not resume_log_path:
        return
    with open(resume_log_path, "a", encoding="utf-8") as fh:
        fh.write(str(file_path) + "\n")


# ═══════════════════════════════════════════════════════════════════════════════
# NEEDS-PROCESSING CHECK
# ═══════════════════════════════════════════════════════════════════════════════

def needs_processing(file_path: Path) -> bool:
    """
    Quick check: return True if the file should be processed.

    A file does NOT need processing if SKIP_IF_AAC_EXISTS is True, it already
    has an English AAC track, AND that track is the sole default audio
    stream. This mirrors process_file()'s own Step 3 check exactly — a file
    can have an AAC track but still need a (cheap, non-transcoding)
    disposition-only remux if the source shipped it with the wrong default
    flag (e.g. a foreign dub flagged default instead). Checking only
    "has AAC" here would silently skip those files forever, since
    process_file() would never even get called.

    Probing is cheap relative to transcoding; this avoids launching ffmpeg
    for files that are already fully in the desired state.

    Returns True (needs processing) if probing fails — process_file() will
    handle the error properly and leave the file untouched.
    """
    if not SKIP_IF_AAC_EXISTS:
        return True  # Config says always re-process
    try:
        probe_data    = probe_file(file_path)
        audio_streams = get_streams_by_type(probe_data, "audio")
        existing_aac  = find_english_aac_track(audio_streams)
        if existing_aac is None:
            return True
        return not audio_dispositions_correct(audio_streams, existing_aac)
    except Exception as exc:
        log.warning("Could not probe %s (%s) — will attempt processing anyway.", file_path, exc)
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# RESULT TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

class BatchResult:
    """Accumulates per-file results for the final summary. Thread-safe."""

    def __init__(self) -> None:
        self._lock      = threading.Lock()
        self.total      = 0
        self.skipped    = 0   # Already had AAC / in resume log
        self.processed  = 0   # Successfully transcoded
        self.failed     = 0   # ffmpeg or validation failure
        self.errors: List[Tuple[Path, str]] = []

    def record_skip(self) -> None:
        with self._lock:
            self.total += 1
            self.skipped += 1

    def record_success(self) -> None:
        with self._lock:
            self.total += 1
            self.processed += 1

    def record_failure(self, path: Path, reason: str) -> None:
        with self._lock:
            self.total += 1
            self.failed += 1
            self.errors.append((path, reason))

    def print_summary(self) -> None:
        log.info("═" * 60)
        log.info("BATCH SUMMARY")
        log.info("  Total files examined : %d", self.total)
        log.info("  Skipped (no change)  : %d", self.skipped)
        log.info("  Successfully processed: %d", self.processed)
        log.info("  Failed               : %d", self.failed)
        if self.errors:
            log.info("  Failed files:")
            for path, reason in self.errors:
                log.info("    ✗ %s — %s", path, reason)
        log.info("═" * 60)


# ═══════════════════════════════════════════════════════════════════════════════
# GRACEFUL SHUTDOWN
# ═══════════════════════════════════════════════════════════════════════════════

_shutdown_requested = False


def _handle_sigint(signum: int, frame: object) -> None:
    """
    Catch Ctrl-C and set a flag so the main loop can exit cleanly after the
    current file finishes.  The in-progress temp file will be cleaned up by
    process_file()'s own error handling when the process eventually exits.
    """
    global _shutdown_requested
    if not _shutdown_requested:
        print(
            "\n[INTERRUPTED] Ctrl-C received — finishing current file then stopping.\n"
            "Already-processed files will not be re-processed (they now have AAC).",
            file=sys.stderr,
        )
    _shutdown_requested = True


# ═══════════════════════════════════════════════════════════════════════════════
# SINGLE-FILE WRAPPER (used by thread pool)
# ═══════════════════════════════════════════════════════════════════════════════

def process_one(
    file_path: Path,
    dry_run: bool,
    resume_log_path: Optional[str],
    result: BatchResult,
) -> None:
    """
    Check and optionally process a single *file_path*.

    In dry-run mode, logs intent without calling ffmpeg.
    On success, appends the path to the resume log if configured.
    Thread-safe: each file gets its own temp path, and BatchResult mutation
    should be done under a lock when using multiple workers.
    """
    # Quick pre-check — avoids probing the file a second time inside process_file()
    # when SKIP_IF_AAC_EXISTS is True.
    if not needs_processing(file_path):
        log.info("SKIP (already has correctly-flagged AAC): %s", file_path)
        result.record_skip()
        return

    if dry_run:
        log.info("DRY-RUN (would process): %s", file_path)
        result.record_success()  # Count as "would succeed" for summary
        return

    success = process_file(file_path)
    if success:
        result.record_success()
        # The file itself is already processed correctly at this point — a
        # failure to update the (purely advisory) resume log must not be
        # reported as a processing failure, or this file gets double-counted
        # (once here as success, once by main()'s outer exception handler).
        try:
            append_to_resume_log(resume_log_path, file_path)
        except OSError as exc:
            log.warning("Processed %s but failed to update resume log: %s", file_path, exc)
    else:
        result.record_failure(file_path, "ffmpeg or validation failure — see log above")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Batch-add AAC stereo dual-audio tracks to a Jellyfin media library.\n"
            "Files that already have an AAC track are skipped automatically."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "library_dir",
        type=Path,
        help="Root directory of the media library to process.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe files and report what would be done without running ffmpeg.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Number of parallel ffmpeg jobs (default: 1). "
            "Each job is CPU- and I/O-intensive — increase with caution."
        ),
    )
    parser.add_argument(
        "--ext",
        nargs="+",
        metavar="EXT",
        help=(
            "File extensions to include (without leading dot, case-insensitive). "
            f"Default: {' '.join(e.lstrip('.') for e in DEFAULT_MEDIA_EXTENSIONS)}"
        ),
    )
    parser.add_argument(
        "--resume-log",
        metavar="PATH",
        help=(
            "Path to a plain-text file that tracks successfully processed files "
            "(one path per line).  Files already in this log are skipped.  "
            "The log is appended to on each successful file."
        ),
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        help="Append all log output to this file in addition to stdout.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug-level logging (very noisy).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Stop after processing N files (useful for testing on a subset).",
    )

    return parser.parse_args()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    global log

    args = parse_args()
    log  = setup_logging(args.log_file, args.verbose)

    log.info("╔══ AAC Library Batch Processor ══╗")
    if args.dry_run:
        log.info("*** DRY-RUN MODE — no files will be modified ***")

    # ── Validate library directory ─────────────────────────────────────────────
    library = args.library_dir.resolve()
    if not library.exists():
        log.error("Library directory not found: %s", library)
        sys.exit(1)
    if not library.is_dir():
        log.error("Path is not a directory: %s", library)
        sys.exit(1)
    log.info("Library root  : %s", library)

    # ── Build extension set ────────────────────────────────────────────────────
    if args.ext:
        extensions = tuple(f".{e.lstrip('.')}" for e in args.ext)
    else:
        extensions = DEFAULT_MEDIA_EXTENSIONS
    log.info("Extensions    : %s", ", ".join(extensions))

    # ── Register Ctrl-C handler ────────────────────────────────────────────────
    signal.signal(signal.SIGINT, _handle_sigint)

    # ── Discover files ─────────────────────────────────────────────────────────
    log.info("Scanning for media files …")
    all_files = find_media_files(library, extensions)
    log.info("Found %d media file(s).", len(all_files))

    # ── Load resume log ────────────────────────────────────────────────────────
    already_done = load_resume_log(args.resume_log)

    # Filter out files listed in resume log.
    if already_done:
        before = len(all_files)
        all_files = [f for f in all_files if str(f) not in already_done]
        log.info("Resume log excluded %d file(s) — %d remain.", before - len(all_files), len(all_files))

    # ── Apply --limit ──────────────────────────────────────────────────────────
    if args.limit and args.limit > 0:
        all_files = all_files[: args.limit]
        log.info("--limit applied — processing at most %d file(s).", args.limit)

    if not all_files:
        log.info("No files to process.")
        sys.exit(0)

    # ── Process ────────────────────────────────────────────────────────────────
    result  = BatchResult()
    workers = max(1, args.workers)
    start   = time.monotonic()

    log.info(
        "Starting processing — workers: %d | files: %d",
        workers, len(all_files),
    )
    log.info("─" * 60)

    if workers == 1:
        # Sequential — simplest, safest, easiest to debug.
        for i, file_path in enumerate(all_files, start=1):
            if _shutdown_requested:
                log.warning("Shutdown requested — stopping after file %d.", i - 1)
                break
            log.info("[%d/%d] %s", i, len(all_files), file_path.name)
            process_one(file_path, args.dry_run, args.resume_log, result)
    else:
        # Concurrent — each file gets its own worker thread.
        # process_file() uses per-file temp paths so there is no shared state
        # between workers except BatchResult (reads/writes are GIL-protected
        # for the simple integer counters we use).
        log.warning(
            "Running %d parallel workers — ensure sufficient CPU and disk I/O bandwidth.",
            workers,
        )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(process_one, fp, args.dry_run, args.resume_log, result): fp
                for fp in all_files
            }
            completed = 0
            for future in as_completed(futures):
                completed += 1
                fp = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    log.error("Unhandled exception for %s: %s", fp, exc)
                    result.record_failure(fp, str(exc))

                if _shutdown_requested:
                    log.warning("Shutdown requested — cancelling remaining futures.")
                    for f in futures:
                        f.cancel()
                    break

    # ── Summary ────────────────────────────────────────────────────────────────
    elapsed = time.monotonic() - start
    log.info("Total elapsed: %.1f seconds (%.1f minutes)", elapsed, elapsed / 60)
    result.print_summary()

    sys.exit(1 if result.failed > 0 else 0)


if __name__ == "__main__":
    main()
