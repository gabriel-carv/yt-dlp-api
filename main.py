from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
import subprocess
import json
import os
import tempfile
import uuid
import urllib.request

app = FastAPI()

# If you bake cookies into the image, copy to /app/cookies.txt
# Or override with an env var in Cloud Run.
COOKIES_PATH = os.getenv("YTDLP_COOKIES_PATH", "/app/cookies.txt")


class Req(BaseModel):
    url: str
    format: str | None = None  # e.g. "bestaudio" or "best"


class MergeReq(BaseModel):
    video_url: str
    audio_url: str
    output_ext: str | None = "mp4"  # mp4 recommended


def ytdlp_base_args() -> list[str]:
    args = ["yt-dlp", "--no-playlist"]
    if COOKIES_PATH and os.path.exists(COOKIES_PATH):
        args += ["--cookies", COOKIES_PATH]
    return args


def run_cmd(args: list[str]) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "command failed",
                "returncode": p.returncode,
                "args": args,
                "stderr": (p.stderr or "")[-4000:],
                "stdout": (p.stdout or "")[-2000:],
            },
        )
    return p


def run_ytdlp(args: list[str]) -> subprocess.CompletedProcess[str]:
    # yt-dlp is just another command; keep error handling consistent
    return run_cmd(args)


def download_to(url: str, path: str) -> None:
    if not url or not url.strip():
        raise HTTPException(status_code=400, detail={"message": "download failed", "error": "empty url", "url": url})
    try:
        urllib.request.urlretrieve(url, path)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"message": "download failed", "error": str(e), "url": url})


def ffprobe_streams(path: str) -> dict:
    p = run_cmd(["ffprobe", "-v", "error", "-show_streams", "-of", "json", path])
    try:
        return json.loads(p.stdout or "{}")
    except Exception:
        return {}


def has_audio(meta: dict) -> bool:
    return any(s.get("codec_type") == "audio" for s in meta.get("streams", []))


def has_video(meta: dict) -> bool:
    return any(s.get("codec_type") == "video" for s in meta.get("streams", []))


@app.get("/health")
def health():
    return {
        "ok": True,
        "cookies_path": COOKIES_PATH,
        "cookies_file_present": bool(COOKIES_PATH and os.path.exists(COOKIES_PATH)),
    }


@app.post("/info")
def info(req: Req):
    p = run_ytdlp(ytdlp_base_args() + ["-J", req.url])
    data = json.loads(p.stdout)
    return {
        "title": data.get("title"),
        "id": data.get("id"),
        "duration": data.get("duration"),
        "webpage_url": data.get("webpage_url"),
        "extractor": data.get("extractor"),
    }


@app.post("/extract")
def extract(req: Req):
    fmt = req.format or "best"
    p = run_ytdlp(ytdlp_base_args() + ["-g", "-f", fmt, req.url])
    direct = [line.strip() for line in (p.stdout or "").splitlines() if line.strip()]
    return {"direct_urls": direct}


@app.post("/merge")
def merge(req: MergeReq):
    if not req.video_url or not req.video_url.strip():
        raise HTTPException(status_code=400, detail="video_url is empty")
    if not req.audio_url or not req.audio_url.strip():
        raise HTTPException(status_code=400, detail="audio_url is empty")

    out_ext = (req.output_ext or "mp4").lower()
    if out_ext not in ("mp4", "mkv"):
        raise HTTPException(status_code=400, detail="output_ext must be mp4 or mkv")

    with tempfile.TemporaryDirectory() as td:
        a_path = os.path.join(td, "a.bin")
        b_path = os.path.join(td, "b.bin")
        out_name = f"merged-{uuid.uuid4().hex}.{out_ext}"
        out_path = os.path.join(td, out_name)

        # Download both inputs
        download_to(req.video_url, a_path)
        download_to(req.audio_url, b_path)

        # Detect streams
        a_meta = ffprobe_streams(a_path)
        b_meta = ffprobe_streams(b_path)

        a_has_v, a_has_a = has_video(a_meta), has_audio(a_meta)
        b_has_v, b_has_a = has_video(b_meta), has_audio(b_meta)

        # If one file already has both audio+video, return it as-is
        if a_has_v and a_has_a:
            with open(a_path, "rb") as f:
                data = f.read()
            return Response(
                content=data,
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
            )

        if b_has_v and b_has_a:
            with open(b_path, "rb") as f:
                data = f.read()
            return Response(
                content=data,
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
            )

        # Identify which input is video and which is audio
        if a_has_v and b_has_a:
            v_in, a_in = a_path, b_path
        elif b_has_v and a_has_a:
            v_in, a_in = b_path, a_path
        else:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "No audio stream found in either input. Your 'audio_url' is probably not audio.",
                    "a_stream_types": [s.get("codec_type") for s in a_meta.get("streams", [])],
                    "b_stream_types": [s.get("codec_type") for s in b_meta.get("streams", [])],
                },
            )

        # Mux without re-encoding
        run_cmd([
            "ffmpeg", "-y",
            "-i", v_in,
            "-i", a_in,
            "-c", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-movflags", "+faststart",
            out_path
        ])

        # IMPORTANT: read the output before the temp directory is cleaned up
        with open(out_path, "rb") as f:
            merged_bytes = f.read()

        return Response(
            content=merged_bytes,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
        )
