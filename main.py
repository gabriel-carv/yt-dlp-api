from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess
import json
import os

app = FastAPI()

# If you bake cookies into the image, copy to /app/cookies.txt
# Or override with an env var in Cloud Run.
COOKIES_PATH = os.getenv("YTDLP_COOKIES_PATH", "/app/cookies.txt")


class Req(BaseModel):
    url: str
    format: str | None = None  # e.g. "bestaudio" or "best"


def ytdlp_base_args() -> list[str]:
    args = ["yt-dlp", "--no-playlist"]
    if COOKIES_PATH and os.path.exists(COOKIES_PATH):
        args += ["--cookies", COOKIES_PATH]
    return args


def run_ytdlp(args: list[str]) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        # Return stderr so you can see what's wrong in Make.com / browser
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
