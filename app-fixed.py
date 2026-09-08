import json
import os
import subprocess
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


def clean_url(value: str) -> str:
    value = (value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("u must be an http(s) URL")
    return value


def parse_seek(value: str | None) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def selected_audio_map(value: str | None) -> list[str]:
    # The API receives the ordinal inside the audio-only list, not ffprobe's
    # global stream index: 0:a:0, 0:a:1, etc.
    if value is None or value == "":
        return ["-map", "0:a:0?"]
    try:
        ordinal = int(value)
    except ValueError:
        return ["-map", "0:a:0?"]
    if ordinal < 0 or ordinal > 32:
        return ["-map", "0:a:0?"]
    return ["-map", f"0:a:{ordinal}?"]


@app.route("/")
def root():
    return jsonify(
        ok=True,
        service="tgnexa-transcode",
        endpoints=["/probe?u=", "/transcode?u=&a=&ss="],
    )


@app.route("/probe")
def probe():
    try:
        url = clean_url(request.args.get("u", ""))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400

    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-user_agent",
        UA,
        url,
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, timeout=30, check=True)
        data = json.loads(completed.stdout.decode("utf-8", "ignore"))
    except subprocess.CalledProcessError as exc:
        return jsonify(ok=False, error=f"ffprobe failed: {exc.stderr.decode('utf-8', 'ignore')[-500:]}"), 502
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="ffprobe timed out"), 504
    except (OSError, json.JSONDecodeError) as exc:
        return jsonify(ok=False, error=str(exc)), 500

    try:
        duration = float(data.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0

    audio, video, subs = [], [], []
    for stream in data.get("streams", []):
        tags = stream.get("tags") or {}
        entry = {
            "index": stream.get("index"),
            "codec": stream.get("codec_name"),
            "language": tags.get("language"),
            "title": tags.get("title"),
            "default": bool((stream.get("disposition") or {}).get("default")),
        }
        stream_type = stream.get("codec_type")
        if stream_type == "audio":
            entry["channels"] = stream.get("channels")
            entry["channel_layout"] = stream.get("channel_layout")
            audio.append(entry)
        elif stream_type == "video":
            entry["width"] = stream.get("width")
            entry["height"] = stream.get("height")
            video.append(entry)
        elif stream_type == "subtitle":
            subs.append(entry)

    return jsonify(ok=True, duration=duration, audio=audio, video=video, subtitle=subs)


@app.route("/transcode")
def transcode():
    try:
        url = clean_url(request.args.get("u", ""))
    except ValueError as exc:
        return Response(str(exc), status=400)

    seek = parse_seek(request.args.get("ss"))
    audio_map = selected_audio_map(request.args.get("a"))

    # Do not stream-copy video here. Stream-copying video while re-encoding
    # audio is what created the different starting timestamps and the audible
    # drift after seeking. Both tracks are now decoded and rebuilt from PTS 0.
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-user_agent",
        UA,
        "-i",
        url,
    ]
    if seek > 0:
        # Accurate output seek: input is decoded through the requested point,
        # instead of jumping to a video keyframe while audio starts elsewhere.
        cmd += ["-ss", f"{seek:.3f}"]

    cmd += [
        "-map",
        "0:v:0?",
        *audio_map,
        # Reset both elementary streams before muxing them together.
        "-vf",
        "setpts=PTS-STARTPTS",
        "-af",
        "aresample=async=1:min_hard_comp=0.100:first_pts=0,asetpts=PTS-STARTPTS",
        # Re-encode both streams so the output has one stable, browser-safe
        # timeline. The preset keeps seek startup practical on small servers.
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "main",
        "-level:v",
        "4.1",
        "-crf",
        "23",
        "-g",
        "48",
        "-keyint_min",
        "48",
        "-sc_threshold",
        "0",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "192k",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof+faststart",
        "-f",
        "mp4",
        "pipe:1",
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError as exc:
        return Response(f"ffmpeg could not start: {exc}", status=502)

    def generate():
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        except GeneratorExit:
            raise
        finally:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    headers = {
        "Content-Type": "video/mp4",
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Access-Control-Allow-Origin": "*",
        "Accept-Ranges": "none",
    }
    return Response(generate(), headers=headers)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
