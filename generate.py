import os, re, sys, json, time, asyncio, subprocess, datetime, pathlib
from types import SimpleNamespace
try:
    import edge_tts
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "edge-tts"], check=True)
    import edge_tts
import requests, feedparser
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from google import genai

# 読みたいサイトの一覧（増やすときはここに1行足す）
# 3つ目は「RSSがないサイト」用の、記事URLの形。RSSがあるサイトは "" のままでOK
SITES = [
    ("民放online", "https://minpo.online/", ""),
    ("POTLUCK YAESU", "https://www.potluck-yaesu.com/", r"/magazine/\d{8}/\d+/"),
]
MAX_PER_SITE = 2   # 1サイトあたり1回に作る最大本数
MAX_TOTAL = 4      # 全体で1回に作る最大本数
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
        if pathlib.Path(x["file"]).stem in gone:
            f = root / x["file"]
            if f.exists():
                f.unlink()
            print("削除:", x["title"])
        else:
            keep.append(x)
    eps = keep
    for p in listened.iterdir():
        if p.is_file():
            p.unlink()


def save():
    db_path.write_text(json.dumps(eps, ensure_ascii=False, indent=1), encoding="utf-8")
    seen_path.write_text(json.dumps(sorted(seen), ensure_ascii=False), encoding="utf-8")


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


def from_page(url, pattern):
    """RSSがないサイト：トップページから記事のリンクを集める"""
    items, got = [], set()
    try:
        s = BeautifulSoup(requests.get(url, headers=UA, timeout=30).text, "html.parser")
    except Exception as ex:
        print("サイトを開けません:", url, ex)
        return items
    for a in s.find_all("a", href=True):
        h = urljoin(url, a["href"]).split("#")[0]
        if re.search(pattern, h) and h not in got:
            got.add(h)
            items.append(SimpleNamespace(link=h, title=a.get_text(" ", strip=True)))
    print(f"記事リンク {len(items)}件:", url)
    return items


def from_feed(url):
    cands = []
    try:
        s = BeautifulSoup(requests.get(url, headers=UA, timeout=30).text, "html.parser")
        for l in s.find_all("link", type=re.compile("rss|atom")):
            cands.append(urljoin(url, l["href"]))
        for a in s.find_all("a"):
            if a.get_text(strip=True).upper() == "RSS" and a.get("href"):
                cands.append(urljoin(url, a["href"]))
    except Exception as ex:
        print("サイトを開けません:", url, ex)
    cands += [urljoin(url, "feed/"), urljoin(url, "rss"), urljoin(url, "feed")]
    for c in cands:
        try:
            f = feedparser.parse(requests.get(c, headers=UA, timeout=30).content)
            if f.entries:
                print("RSS:", c)
                return [e for e in f.entries if e.get("link")]
        except Exception:
            pass
    print("RSSが見つかりませんでした:", url)
    return []


def clean_title(t):
    return re.sub(r"(?:\s[-–]\s|\s*[|｜]\s*)[^-–|｜]+$", "", t or "").strip()


todo = []
for name, url, pattern in SITES:
    items = from_page(url, pattern) if pattern else from_feed(url)
    new = [e for e in items if e.link not in seen][:MAX_PER_SITE]
    todo += [(name, e) for e in new]
todo = todo[:MAX_TOTAL]

for i, (name, e) in enumerate(reversed(todo)):
    try:
        page = BeautifulSoup(requests.get(e.link, headers=UA, timeout=30).text, "html.parser")
        og = page.find("meta", property="og:title")
        title = clean_title(og["content"] if og and og.get("content") else e.title)
        for t in page(["script", "style", "nav", "header", "footer"]):
            t.decompose()
        text = (page.find("article") or page.find("main") or page.body).get_text("\n", strip=True)[:20000]
        prompt = (
            "次の記事を、通勤中に耳で聞いて理解できる日本語の解説原稿にしてください。"
            "読み上げて約5分（1500字前後）。見出しや記号は使わず、話し言葉で。"
            "最初に記事のタイトルを紹介し、要点、背景、地方の放送局やビジネスにとってのヒントの順で話してください。"
            "原稿の本文だけを出力してください。\n\n"
            f"サイト: {name}\nタイトル: {title}\n本文:\n{text}"
        )
        script = write_script(prompt)
        if not script:
            print("原稿を作れずスキップ:", title)
            continue
        fname = datetime.datetime.now().strftime("%Y%m%d%H%M%S") + f"_{i}"
        mp3 = audio_dir / f"{fname}.mp3"
        if not speak(script, mp3):
            print("音声を作れずスキップ:", title)
            continue
        eps.insert(0, {"title": f"{title} - {name}", "site": name, "link": e.link,
                       "file": f"audio/{fname}.mp3", "date": datetime.date.today().isoformat()})
        seen.add(e.link)
        save()
        print("完成:", name, title)
    except Exception as ex:
        print("スキップ:", e.link, ex)

save()
