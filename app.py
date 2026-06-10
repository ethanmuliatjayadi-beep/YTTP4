import contextlib
import io
import re
import shutil
import subprocess
import threading
import unicodedata
import sys
import warnings
from pathlib import Path
from typing import Iterable, List
from urllib.parse import urlparse

try:
    from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
except ImportError as exc:
    raise SystemExit("Missing dependency: install Flask with `pip install flask`.") from exc

try:
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")
    import yt_dlp
    from yt_dlp.utils import DownloadError
except ImportError as exc:
    raise SystemExit("Missing dependency: install yt-dlp with `pip install yt-dlp`.") from exc


APP_DIR = Path(__file__).resolve().parent
CHUNK_SIZE = 64 * 1024
STDERR_LIMIT = 32 * 1024

FORMAT_CONFIG = {
    "mp4": {
        "extension": "mp4",
        "mime": "video/mp4",
        "selector": "best[ext=mp4][vcodec!=none][acodec!=none]/18",
    },
    "mp3": {
        "extension": "mp3",
        "mime": "audio/mpeg",
        "selector": "bestaudio[ext=m4a]/bestaudio/best",
    },
}


app = Flask(__name__)


class ProcessingError(Exception):
    pass


@app.get("/")
def index() -> Response:
    return send_from_directory(APP_DIR, "index.html")


@app.post("/process-stream")
def process_stream() -> Response:
    payload = request.get_json(silent=True) or request.form
    target_url = (payload.get("url") or "").strip()
    requested_format = (payload.get("format") or "").strip().lower()

    if requested_format not in FORMAT_CONFIG:
        return jsonify({"error": "Only MP4 (Video) and MP3 (Audio) conversions are supported."}), 400

    if not is_youtube_url(target_url):
        return jsonify({"error": "Only YouTube links are supported."}), 400

    ffmpeg_path = shutil.which("ffmpeg")
    if requested_format == "mp3" and not ffmpeg_path:
        return jsonify({"error": "ffmpeg is required for file-less MP3 streaming."}), 500

    try:
        info = extract_youtube_info(target_url, requested_format)
        title = info.get("title") or "youtube_download"
        filename = sanitize_filename(title, FORMAT_CONFIG[requested_format]["extension"])
        byte_stream = open_download_stream(target_url, requested_format, ffmpeg_path)
    except DownloadError:
        app.logger.exception("yt-dlp could not extract the requested YouTube URL.")
        return jsonify({"error": "Could not extract media from that YouTube link."}), 502
    except ProcessingError as exc:
        app.logger.exception("Media processing failed.")
        return jsonify({"error": str(exc)}), 502
    except Exception:
        app.logger.exception("Unexpected processing failure.")
        return jsonify({"error": "Unable to process that YouTube link."}), 500

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    return Response(
        stream_with_context(byte_stream),
        mimetype=FORMAT_CONFIG[requested_format]["mime"],
        headers=headers,
        direct_passthrough=True,
    )


def is_youtube_url(raw_url: str) -> bool:
    try:
        parsed = urlparse(raw_url)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        host == "youtu.be"
        or host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtube-nocookie.com"
        or host.endswith(".youtube-nocookie.com")
    )


def extract_youtube_info(target_url: str, requested_format: str) -> dict:
    ydl_options = {
        "format": FORMAT_CONFIG[requested_format]["selector"],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }

    with contextlib.redirect_stderr(io.StringIO()):
        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(target_url, download=False)

    if not isinstance(info, dict) or info.get("_type") == "playlist" or info.get("entries"):
        raise ProcessingError("Please paste a single YouTube video link.")

    if not info.get("title"):
        raise ProcessingError("Could not read the YouTube video title.")

    return info


def sanitize_filename(title: str, extension: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    safe_title = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", ascii_title)
    safe_title = re.sub(r"\s+", " ", safe_title).strip(" ._-")
    safe_title = safe_title[:160].strip(" ._-") or "youtube_download"
    return f"{safe_title}.{extension}"


def open_download_stream(target_url: str, requested_format: str, ffmpeg_path: str) -> Iterable[bytes]:
    ytdlp_command = build_ytdlp_stdout_command(target_url, requested_format)

    if requested_format == "mp4":
        return open_process_stream([ytdlp_command])

    ffmpeg_command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "192k",
        "-f",
        "mp3",
        "pipe:1",
    ]
    return open_process_stream([ytdlp_command, ffmpeg_command])


def build_ytdlp_stdout_command(target_url: str, requested_format: str) -> List[str]:
    return [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--no-progress",
        "--no-cache-dir",
        "--extractor-args",
        "youtube:player_client=android",
        "--retries",
        "10",
        "--fragment-retries",
        "10",
        "--socket-timeout",
        "60",
        "--format",
        FORMAT_CONFIG[requested_format]["selector"],
        "--output",
        "-",
        target_url,
    ]


def open_process_stream(commands: List[List[str]]) -> Iterable[bytes]:
    processes = []
    stderr_tails = []

    try:
        first_process = subprocess.Popen(
            commands[0],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        processes.append(first_process)

        if len(commands) == 1:
            output_process = first_process
        else:
            output_process = subprocess.Popen(
                commands[1],
                stdin=first_process.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            processes.append(output_process)
            if first_process.stdout:
                first_process.stdout.close()
    except OSError as exc:
        stop_processes(processes)
        wait_processes(processes)
        raise ProcessingError("Could not start the streaming pipeline.") from exc

    stderr_threads = []
    for process in processes:
        stderr_tail = bytearray()
        stderr_tails.append(stderr_tail)
        stderr_thread = threading.Thread(target=drain_stderr, args=(process, stderr_tail), daemon=True)
        stderr_thread.start()
        stderr_threads.append(stderr_thread)

    assert output_process.stdout is not None
    first_chunk = output_process.stdout.read(CHUNK_SIZE)
    if not first_chunk:
        stop_processes(processes)
        wait_processes(processes)
        join_threads(stderr_threads)
        message = collect_stderr(stderr_tails)
        raise ProcessingError(message or "The streaming pipeline did not produce a downloadable file.")

    def generate() -> Iterable[bytes]:
        client_aborted = False
        try:
            yield first_chunk
            while True:
                chunk = output_process.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        except GeneratorExit:
            client_aborted = True
            stop_processes(processes)
            raise
        finally:
            if output_process.stdout:
                output_process.stdout.close()
            return_codes = wait_processes(processes)
            join_threads(stderr_threads)
            failed = [code for code in return_codes if code not in (0, None)]
            if failed and not client_aborted:
                app.logger.error("Streaming pipeline exited with %s: %s", failed, collect_stderr(stderr_tails))

    return generate()


def drain_stderr(process: subprocess.Popen, stderr_tail: bytearray) -> None:
    assert process.stderr is not None
    while True:
        chunk = process.stderr.read(4096)
        if not chunk:
            break
        stderr_tail.extend(chunk)
        if len(stderr_tail) > STDERR_LIMIT:
            del stderr_tail[:-STDERR_LIMIT]


def collect_stderr(stderr_tails: List[bytearray]) -> str:
    messages = [clean_stderr(tail.decode("utf-8", errors="replace")).strip() for tail in stderr_tails if tail]
    return "\n".join(message for message in messages if message)


def clean_stderr(message: str) -> str:
    ignored_fragments = (
        "NotOpenSSLWarning",
        "warnings.warn(",
        "urllib3/__init__.py",
        "Deprecated Feature: Support for Python version",
    )
    lines = []
    for line in message.splitlines():
        if any(fragment in line for fragment in ignored_fragments):
            continue
        lines.append(line)
    return "\n".join(lines)


def stop_processes(processes: List[subprocess.Popen]) -> None:
    for process in reversed(processes):
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass


def wait_processes(processes: List[subprocess.Popen]) -> List[int]:
    return [process.wait() for process in processes]


def join_threads(threads: List[threading.Thread]) -> None:
    for thread in threads:
        thread.join(timeout=1)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
