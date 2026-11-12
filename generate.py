import os, re, json, html, wave, subprocess, datetime, pathlib
import requests, feedparser
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

SITE = "https://minpo.online/"
MAX_NEW = 3  # 1回に音声化する最大本数
UA = {"User-Agent": "Mozilla/5.0"}
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

root = pathlib.Path("docs")
audio_dir = root / "audio"
audio_dir.mkdir(parents=True, exist_ok=True)
db_path = root / "episodes.json"
eps = json.loads(db_path.read_text(encoding="utf-8")) if db_path.exists() else []
done = {e["link"] for e in eps}

def find_feed():
    s = BeautifulSoup(requests.get(SITE, headers=UA, timeout=30).text, "html.parser")
    for l in s.find_all("link", type=re.compile("rss|atom")):
        return urljoin(SITE, l["href"])
    for a in s.find_all("a"):
        if a.get_text(strip=True).upper() == "RSS":
            return urljoin(SITE, a["href"])
    raise SystemExit("RSSが見つかりませんでした")

feed = feedparser.parse(requests.get(find_feed(), headers=UA, timeout=30).content)
new = [e for e in feed.entries if e.link not in done][:MAX_NEW]

for i, e in enumerate(reversed(new)):
    page = BeautifulSoup(requests.get(e.link, headers=UA, timeout=30).text, "html.parser")
    for t in page(["script", "style", "nav", "header", "footer"]):
        t.decompose()
    body = (page.find("article") or page.body).get_text("\n", strip=True)[:20000]

    prompt = (
        "次の記事を、通勤中に耳で聞いて理解できる日本語の解説原稿にしてください。"
        "読み上げて約5分（1500字前後）。見出しや記号は使わず、話し言葉で。"
        "最初に記事のタイトルを紹介し、要点、背景、放送業界にとっての意味の順で話してください。\n\n"
        f"タイトル: {e.title}\n本文:\n{body}"
    )
    script = client.models.generate_content(model="gemini-2.5-flash", contents=prompt).text

    res = client.models.generate_content(
        model="gemini-2.5-flash-preview-tts",
        contents=script,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
                )
            ),
        ),
    )
    pcm = res.candidates[0].content.parts[0].inline_data.data

    name = datetime.datetime.now().strftime("%Y%m%d%H%M%S") + f"_{i}"
    wav = f"/tmp/{name}.wav"
    with wave.open(wav, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-b:a", "64k",
                    str(audio_dir / f"{name}.mp3")], check=True)

    eps.insert(0, {"title": e.title, "link": e.link, "file": f"audio/{name}.mp3",
                   "date": datetime.date.today().isoformat()})

db_path.write_text(json.dumps(eps, ensure_ascii=False, indent=1), encoding="utf-8")

items = "\n".join(
    f''
    for x in eps
)
(root / "index.html").write_text(f"""<!doctype html>""", encoding="utf-8")
