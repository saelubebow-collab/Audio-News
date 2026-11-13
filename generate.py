import os, re, sys, json, html, time, asyncio, subprocess, datetime, pathlib
try:
    import edge_tts
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "edge-tts"], check=True)
    import edge_tts
import requests, feedparser
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from google import genai

SITE = "https://minpo.online/"
REPO = "saelubebow-collab/Audio-News"
MAX_NEW = 4
TEXT_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
VOICE = "ja-JP-NanamiNeural"
UA = {"User-Agent": "Mozilla/5.0"}

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
root = pathlib.Path("docs")
audio_dir = root / "audio"
audio_dir.mkdir(parents=True, exist_ok=True)
db_path = root / "episodes.json"
seen_path = root / "seen.json"
eps = json.loads(db_path.read_text(encoding="utf-8")) if db_path.exists() else []
seen = set(json.loads(seen_path.read_text(encoding="utf-8"))) if seen_path.exists() else set()
seen |= {e["link"] for e in eps}
dead = set()

listened = pathlib.Path("listened")
if listened.exists():
    gone = {p.name for p in listened.iterdir() if p.is_file()}
    keep = []
    for x in eps:
        if pathlib.Path(x["file"]).
