from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import subprocess
import json
import os
import tempfile
import uuid
import urllib.request

app = FastAPI()

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


def run_ytdlp(args: list[str]) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "yt-dlp failed",
                "returncode": p.returncode,
                "args": args,
                "stderr": (p.stderr or "")[-4000:],
                "stdout": (p.stdout or "")[-2000:],
            },
        )
    return p


def run_cmd(args: list[str]) -> None:
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


def download_to(url: str, path: str) -> None:
    # Simple downloader; direct URLs often need headers, but many work as-is.
    # If you hit 403 on downloads, we can add User-Agent/Referer support.
    try:
        urllib.request.urlretrieve(url, path)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"message": "download failed", "error": str(e), "url": url})


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
    # Downloads + ffmpeg mux (no re-encode) to a single MP4
    # Works for typical: video=mp4, audio=m4a
    out_ext = (req.output_ext or "mp4").lower()
    if out_ext not in ("mp4", "mkv"):
        raise HTTPException(status_code=400, detail="output_ext must be mp4 or mkv")

    with tempfile.TemporaryDirectory() as td:
        video_path = os.path.join(td, "video.bin")
        audio_path = os.path.join(td, "audio.bin")
        out_name = f"merged-{uuid.uuid4().hex}.{out_ext}"
        out_path = os.path.join(td, out_name)

        download_to(req.video_url, video_path)
        download_to(req.audio_url, audio_path)

        # Mux without re-encoding.
        # +faststart makes MP4 streamable.
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-movflags", "+faststart",
            out_path
        ]
        run_cmd(cmd)

        # Return the merged file as the HTTP response body
        return FileResponse(
            out_path,
            media_type="application/octet-stream",
            filename=out_name,
        )
