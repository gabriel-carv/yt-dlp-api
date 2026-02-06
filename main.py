from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import subprocess
import json
import os

app = FastAPI()

# Optional: set this in Cloud Run as an environment variable.
# If set, requests must include header: X-API-Key: <value>
API_KEY = os.getenv("API_KEY", "").strip()

# Optional: mount a cookies file (e.g. from Secret Manager) at this path.
# If it exists, yt-dlp will use it (helps a lot for Instagram).
COOKIES_PATH = os.getenv("YTDLP_COOKIES_PATH", "/secrets/cookies.txt")


class Req(BaseModel):
    url: str
    format: str | None = None  # e.g. "bestaudio" or "best"


def require_key(x_api_key: str | None):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


def build_ytdlp_base_args() -> list[str]:
    args = ["yt-dlp", "--no-playlist"]
    # Use cookies if the file exists
    if COOKIES_PATH and os.path.exists(COOKIES_PATH):
        args += ["--cookies", COOKIES_PATH]
    return args


def run_ytdlp(args: list[str]) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        # Return a helpful error (instead of a 500) so you can debug in Make.com
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
        "cookies_file_present": bool(COOKIES_PATH and os.path.exists(COOKIES_PATH)),
        "api_key_required": bool(API_KEY),
    }


@app.post("/info")
def info(req: Req, x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    require_key(x_api_key)

    args = build_ytdlp_base_args() + ["-J", req.url]
    p = run_ytdlp(args)

    data = json.loads(p.stdout)
    return {
        "title": data.get("title"),
        "id": data.get("id"),
        "duration": data.get("duration"),
        "webpage_url": data.get("webpage_url"),
        "extractor": data.get("extractor"),
    }


@app.post("/extract")
def extract(req: Req, x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    require_key(x_api_key)

    fmt = req.format or "best"
    args = build_ytdlp_base_args() + ["-g", "-f", fmt, req.url]
    p = run_ytdlp(args)

    direct = [line.strip() for line in (p.stdout or "").splitlines() if line.strip()]
    return {"direct_urls": direct}
