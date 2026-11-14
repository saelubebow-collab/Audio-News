import os, re, sys, json, time, asyncio, subprocess, datetime, pathlib
from types import SimpleNamespace
try:
    import edge_tts
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "edge-tts"], check=True)
    import edge_tts
import requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from google import genai

# 全国紙の「朝のニュースまとめ」（新聞ごとに約5分）
# 3つ目は、その新聞の記事URLの形
PAPERS = [
    ("読売新聞", "https://www.yomiuri.co.jp/", r"/\d{8}-OYT1T\d+"),
    ("朝日新聞", "https://www.asahi.com/", r"/articles/[A-Z0-9]{8,}"),
    ("毎日新聞", "https://mainichi.jp/", r"/articles/\d{8}/"),
    ("産経新聞", "https://www.sankei.com/", r"/article/\d{8}-"),
]
GROUP = "paper"        # ページの「全国紙」タブに出す
NEWS_PER_PAPER = 8     # 1つの新聞から拾うニュースの数
TEXT_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
VOICE = "ja-JP-NanamiNeural"
UA = {"User-Agent": "Mozilla/5.0"}

JST = datetime.timezone(datetime.timedelta(hours=9))
NOW = datetime.datetime.now(JST)
TODAY = NOW.date()

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
root = pathlib.Path("docs")
audio_dir = root / "audio"
audio_dir.mkdir(parents=True, exist_ok=True)
db_path = root / "news.json"
items = json.loads(db_path.read_text(encoding="utf-8")) if db_path.exists() else []
dead = set()


def save():
    db_path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def broken(t):
    return bool(re.search("[\u00e0-\u00ef][\u0080-\u00bf]{2}", t or ""))


def clean_title(t):
    return re.sub(r"(?:\s[-–]\s|\s*[|｜]\s*)[^-–|｜]+$", "", t or "").strip()


def meta(p, key):
    m = p.find("meta", property=key) or p.find("meta", attrs={"name": key})
    return (m.get("content") or "").strip() if m else ""


def soup(url):
    return BeautifulSoup(requests.get(url, headers=UA, timeout=30).content, "html.parser")


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


def links(url, pattern):
    out, got = [], set()
    try:
        s = soup(url)
    except Exception as ex:
        print("サイトを開けません:", url, ex)
        return out
    for a in s.find_all("a", href=True):
        h = urljoin(url, a["href"]).split("#")[0].split("?")[0]
        if re.search(pattern, h) and h not in got:
            got.add(h)
            out.append(SimpleNamespace(link=h, title=a.get_text(" ", strip=True)))
    print(f"記事リンク {len(out)}件:", url)
    return out


def digest(n, name, url, pattern):
    global items
    if any(x["site"] == name and x["date"] == TODAY.isoformat() for x in items):
        print("今日のまとめは作成済み:", name)
        return
    news = []
    for e in links(url, pattern)[:NEWS_PER_PAPER]:
        try:
            p = soup(e.link)
            t = clean_title(meta(p, "og:title") or e.title)
            d = meta(p, "og:description") or meta(p, "description")
            if t and not broken(t):
                news.append(f"・見出し：{t}\n　冒頭：{d}")
        except Exception as ex:
            print("記事を開けません:", e.link, ex)
    print(f"{name}：ニュース {len(news)}本")
    if len(news) < 3:
        print("ニュースが少ないのでスキップ:", name)
        return
    prompt = (
        f"あなたはラジオのニュース解説者です。以下は、{name}のサイトに今朝掲載されている主なニュースの見出しと冒頭部分です。"
        "これをもとに、「ながら日経」のように、通勤中に耳で聞いて分かる約5分（1400〜1600字）の朝のニュース解説原稿を作ってください。"
        f"最初に「{TODAY.month}月{TODAY.day}日、{name}の朝のニュースをお伝えします」と始め、"
        "重要そうな順に5〜7本を、何が起きたか、ポイントは何か、の流れでかいつまんで話してください。"
        "見出しと冒頭に書かれていないことは、推測で付け足さないでください。最後にひとことでまとめてください。"
        "見出しや記号は使わず話し言葉で、原稿の本文だけを出力してください。\n\n" + "\n".join(news)
    )
    script = write_script(prompt)
    if not script:
        print("原稿を作れずスキップ:", name)
        return
    fname = "news_" + NOW.strftime("%Y%m%d%H%M%S") + f"_{n}"
    mp3 = audio_dir / f"{fname}.mp3"
    if not speak(script, mp3):
        print("音声を作れずスキップ:", name)
        return
    # 同じ新聞の前の日のまとめは片付ける
    for x in [x for x in items if x["site"] == name]:
        f = root / x["file"]
        if f.exists():
            f.unlink()
    items = [x for x in items if x["site"] != name]
    items.append({"title": "朝のニュースまとめ", "site": name, "group": GROUP, "count": len(news),
                  "link": url, "file": f"audio/{fname}.mp3", "date": TODAY.isoformat()})
    save()
    print("完成: 朝のまとめ", name)


for n, (name, url, pattern) in enumerate(PAPERS):
    try:
        digest(n, name, url, pattern)
    except Exception as ex:
        print("まとめを作れません:", name, ex)

order = [p[0] for p in PAPERS]
items.sort(key=lambda x: order.index(x["site"]) if x["site"] in order else 99)
save()
