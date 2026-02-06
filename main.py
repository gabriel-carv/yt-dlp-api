from fastapi import FastAPI
from pydantic import BaseModel
import subprocess, json

app = FastAPI()

class Req(BaseModel):
    url: str
    format: str | None = None  # e.g. "bestaudio" or "best"

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/info")
def info(req: Req):
    p = subprocess.run(["yt-dlp", "-J", req.url],
                       capture_output=True, text=True, check=True)
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
    p = subprocess.run(["yt-dlp", "-g", "-f", fmt, req.url],
                       capture_output=True, text=True, check=True)
    direct = [line.strip() for line in p.stdout.splitlines() if line.strip()]
    return {"direct_urls": direct}