from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess
import json
import os
import tempfile
import uuid
import urllib.request
from datetime import timedelta

from google.cloud import storage

app = FastAPI()

# Cookies (optional)
COOKIES_PATH = os.getenv("YTDLP_COOKIES_PATH", "/app/cookies.txt")

# GCS settings
MERGE_BUCKET = os.getenv("MERGE_BUCKET", "").strip()  # required for /merge
SIGNED_URL_MINUTES = int(os.getenv("SIGNED_URL_MINUTES", "15"))
GCS_PREFIX = os.getenv("GCS_PREFIX", "merged/").strip()  # optional folder prefix in bucket


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


def upload_and_sign(local_path: str, object_name: str, content_type: str) -> dict:
    if not MERGE_BUCKET:
        raise HTTPException(
            status_code=500,
            detail="MERGE_BUCKET env var is not set. Set it to your GCS bucket name.",
        )

    client = storage.Client()
    bucket = client.bucket(MERGE_BUCKET)
    blob = bucket.blob(object_name)

    blob.upload_from_filename(local_path, content_type=content_type)

    gs_uri = f"gs://{MERGE_BUCKET}/{object_name}"

    # Signed URL (temporary). This usually works on Cloud Run without any key file,
    # as long as the runtime service account can sign URLs.
    download_url = None
    try:
        download_url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=SIGNED_URL_MINUTES),
            method="GET",
        )
    except Exception as e:
        # If signing isn't permitted, still return gs:// and object name
        return {
            "ok": True,
            "gs_uri": gs_uri,
            "object_name": object_name,
            "download_url": None,
            "note": "Upload succeeded but signed URL generation failed. See 'sign_error'.",
            "sign_error": str(e),
        }

    return {
        "ok": True,
        "gs_uri": gs_uri,
        "object_name": object_name,
        "download_url": download_url,
        "expires_in_minutes": SIGNED_URL_MINUTES,
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "cookies_path": COOKIES_PATH,
        "cookies_file_present": bool(COOKIES_PATH and os.path.exists(COOKIES_PATH)),
        "merge_bucket_set": bool(MERGE_BUCKET),
        "merge_bucket": MERGE_BUCKET or None,
        "gcs_prefix": GCS_PREFIX,
        "signed_url_minutes": SIGNED_URL_MINUTES,
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

    # We will always upload MP4/MKV to GCS and return a signed link
    content_type = "video/mp4" if out_ext == "mp4" else "video/x-matroska"

    with tempfile.TemporaryDirectory() as td:
        a_path = os.path.join(td, "a.bin")
        b_path = os.path.join(td, "b.bin")
        out_name = f"merged-{uuid.uuid4().hex}.{out_ext}"
        out_path = os.path.join(td, out_name)

        download_to(req.video_url, a_path)
        download_to(req.audio_url, b_path)

        a_meta = ffprobe_streams(a_path)
        b_meta = ffprobe_streams(b_path)

        a_has_v, a_has_a = has_video(a_meta), has_audio(a_meta)
        b_has_v, b_has_a = has_video(b_meta), has_audio(b_meta)

        # If one already has both audio+video, upload that file directly
        if a_has_v and a_has_a:
            object_name = f"{GCS_PREFIX}{out_name}"
            return upload_and_sign(a_path, object_name, content_type)

        if b_has_v and b_has_a:
            object_name = f"{GCS_PREFIX}{out_name}"
            return upload_and_sign(b_path, object_name, content_type)

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

        object_name = f"{GCS_PREFIX}{out_name}"
        return upload_and_sign(out_path, object_name, content_type)
