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
MAX_NEW = 4  # 1回に音声化する最大本数
TEXT_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
VOICE = "ja-JP-NanamiNeural"  # 男性の声なら ja-JP-KeitaNeural
UA = {"User-Agent": "Mozilla/5.0"}

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
root = pathlib.Path("docs")
audio_dir = root / "audio"
audio_dir.mkdir(parents=True, exist_ok=True)
db_path = root / "episodes.json"
eps = json.loads(db_path.read_text(encoding="utf-8")) if db_path.exists() else []
done = {e["link"] for e in eps}
dead = set()


def write_script(prompt):
    for _ in range(3):
        for m in TEXT_MODELS:
            if m in dead:
                continue
            try:
                r = client.models.generate_content(model=m, contents=prompt)
                if r.text:
                    return r.text
            except Exception as ex:
                print(f"{m} 失敗: {str(ex)[:300]}")
                if "404" in str(ex):
                    dead.add(m)
        time.sleep(60)
    return None


def speak(script, path):
    for _ in range(3):
        try:
            asyncio.run(edge_tts.Communicate(script, VOICE).save(str(path)))
            if path.exists() and path.stat().st_size > 1000:
                return True
        except Exception as ex:
            print("読み上げ失敗:", str(ex)[:300])
        time.sleep(30)
    return False


def find_feed():
    s = BeautifulSoup(requests.get(SITE, headers=UA, timeout=30).text, "html.parser")
    for l in s.find_all("link", type=re.compile("rss|atom")):
        return urljoin(SITE, l["href"])
    for a in s.find_all("a"):
        if a.get_text(strip=True).upper() == "RSS":
            return urljoin(SITE, a["href"])
    raise SystemExit("RSSが見つかりませんでした")


def build_page():
    cards = []
    for x in eps:
        cards.append(
            f'<li><p class="d">{x["date"]}</p><h2>{html.escape(x["title"])}</h2>'
            f'<audio controls preload="none" src="{x["file"]}"></audio>'
            f'<a href="{html.escape(x["link"])}" target="_blank">元の記事を読む</a></li>'
        )
    body = "\n".join(cards) or "<p>まだ音声はありません。</p>"
    page = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>音声ニュース</title>
<style>
body{{font-family:-apple-system,sans-serif;margin:0;padding:16px;background:#f6f6f4;color:#222}}
@media (prefers-color-scheme:dark){{body{{background:#111;color:#eee}}li{{background:#1c1c1c!important}}}}
h1{{font-size:22px}} ul{{list-style:none;padding:0}}
li{{background:#fff;border-radius:12px;padding:14px;margin-bottom:12px}}
h2{{font-size:17px;margin:4px 0 10px}} .d{{font-size:13px;opacity:.6;margin:0}}
audio{{width:100%}} a{{display:inline-block;margin-top:8px;font-size:14px;color:#3a7bd5}}
</style></head><body>
<h1>新着の音声解説</h1><ul>
{body}
</ul></body></html>"""
    (root / "index.html").write_text(page, encoding="utf-8")


feed = feedparser.parse(requests.get(find_feed(), headers=UA, timeout=30).content)
new = [e for e in feed.entries if e.link not in done][:MAX_NEW]

for i, e in enumerate(reversed(new)):
    try:
        page = BeautifulSoup(requests.get(e.link, headers=UA, timeout=30).text, "html.parser")
        for t in page(["script", "style", "nav", "header", "footer"]):
            t.decompose()
        text = (page.find("article") or page.body).get_text("\n", strip=True)[:20000]
        prompt = (
            "次の記事を、通勤中に耳で聞いて理解できる日本語の解説原稿にしてください。"
            "読み上げて約5分（1500字前後）。見出しや記号は使わず、話し言葉で。"
            "最初に記事のタイトルを紹介し、要点、背景、放送業界にとっての意味の順で話してください。"
            "原稿の本文だけを出力してください。\n\n"
            f"タイトル: {e.title}\n本文:\n{text}"
        )
        script = write_script(prompt)
        if not script:
            print("原稿を作れずスキップ:", e.title)
            continue
        name = datetime.datetime.now().strftime("%Y%m%d%H%M%S") + f"_{i}"
        mp3 = audio_dir / f"{name}.mp3"
        if not speak(script, mp3):
            print("音声を作れずスキップ:", e.title)
            continue
        eps.insert(0, {"title": e.title, "link": e.link, "file": f"audio/{name}.mp3",
                       "date": datetime.date.today().isoformat()})
        db_path.write_text(json.dumps(eps, ensure_ascii=False, indent=1), encoding="utf-8")
        print("完成:", e.title)
    except Exception as ex:
        print("スキップ:", e.link, ex)

build_page()
