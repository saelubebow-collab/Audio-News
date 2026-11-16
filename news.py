import os, re, sys, json, time, asyncio, subprocess, datetime, pathlib
from types import SimpleNamespace
try:
    import edge_tts
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "edge-tts"], check=True)
    import edge_tts
import requests
from urllib.parse import urljoin, urlparse, quote
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from google import genai

# 全国紙（トップページから拾う。読み取りが許可されていなければGoogleニュースの見出し）
# 3つ目は、その新聞の記事URLの形
PAPERS = [
    ("読売新聞", "https://www.yomiuri.co.jp/", r"/\d{8}-OYT1T\d+"),
    ("朝日新聞", "https://www.asahi.com/", r"/articles/[A-Z0-9]{8,}"),
    ("毎日新聞", "https://mainichi.jp/", r"/articles/\d{8}/"),
    ("産経新聞", "https://www.sankei.com/", r"/article/\d{8}-"),
]
# 地元紙（Yahoo!ニュースの提供社ごとのRSSから拾う）
LOCALS = [
    ("北日本新聞", "kitanihon"),
    ("北國新聞", "hokkoku"),
    ("新潟日報", "niigatan"),
]
# 経済メディア（RSSから拾う。RSSが読めなければGoogleニュースの見出し）
BIZ = [
    ("東洋経済", "https://toyokeizai.net/", "https://toyokeizai.net/list/feed/rss"),
    ("PRESIDENT Online", "https://president.jp/", "https://president.jp/list/rss"),
    ("時事通信", "https://www.jiji.com/", ""),
]
NEWS_PER_PAPER = 7     # 1つの新聞から拾うニュースの数
TEXT_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
VOICE = "ja-JP-NanamiNeural"
UA = {"User-Agent": "Mozilla/5.0"}

JST = datetime.timezone(datetime.timedelta(hours=9))
NOW = datetime.datetime.now(JST)
TODAY = NOW.date()
SLOT = "am" if NOW.hour < 12 else "pm"          # 朝5時の回か、夕方16時の回か
WHEN = "朝" if SLOT == "am" else "夕方"

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
root = pathlib.Path("docs")
audio_dir = root / "audio"
audio_dir.mkdir(parents=True, exist_ok=True)
db_path = root / "news.json"
used_path = root / "news_used.json"
items = json.loads(db_path.read_text(encoding="utf-8")) if db_path.exists() else []
used = json.loads(used_path.read_text(encoding="utf-8")) if used_path.exists() else []
dead = set()
robots = {}


def save():
    db_path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    used_path.write_text(json.dumps(used[-400:], ensure_ascii=False), encoding="utf-8")


def broken(t):
    return bool(re.search("[à-ï][\u0080-¿]{2}", t or ""))


def clean_title(t):
    t = re.sub(r"（[^（）]{2,20}）$", "", (t or "").strip())
    return re.sub(r"(?:\s[-–]\s|\s*[|｜]\s*)[^-–|｜]+$", "", t).strip()


def allowed(url):
    """サイトが自動の読み取りを許可しているか（robots.txt）を確かめる"""
    u = urlparse(url)
    host = f"{u.scheme}://{u.netloc}"
    if host not in robots:
        rp = RobotFileParser()
        try:
            r = requests.get(host + "/robots.txt", headers=UA, timeout=20)
            if r.status_code in (401, 403):
                rp.disallow_all = True
            elif r.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(r.text.splitlines())
        except Exception:
            rp.disallow_all = True
        robots[host] = rp
    ok = robots[host].can_fetch("*", url)
    if not ok:
        print("読み取り不可（サイトのルール）:", url)
    return ok


def meta(p, key):
    m = p.find("meta", property=key) or p.find("meta", attrs={"name": key})
    return (m.get("content") or "").strip() if m else ""


def soup(url):
    return BeautifulSoup(requests.get(url, headers=UA, timeout=30).content, "html.parser")


def read_article(url, limit=600):
    """記事のページで、無料で読める部分を少しだけ取ってくる（許可されている場合だけ）"""
    if not url or not allowed(url):
        return "", ""
    try:
        p = soup(url)
        title = clean_title(meta(p, "og:title"))
        d = meta(p, "og:description") or meta(p, "description")
        for t in p(["script", "style", "nav", "header", "footer", "aside"]):
            t.decompose()
        body = p.find("article") or p.find("main")
        text = body.get_text(" ", strip=True) if body else ""
        return ("" if broken(title) else title), (d + " " + text).strip()[:limit]
    except Exception as ex:
        print("記事を開けません:", url, ex)
        return "", ""


def rss_items(feed):
    out = []
    try:
        x = ET.fromstring(requests.get(feed, headers=UA, timeout=30).content)
        for it in x.iter():
            if not isinstance(it.tag, str) or it.tag.split("}")[-1] != "item":
                continue
            f = {c.tag.split("}")[-1]: (c.text or "") for c in it}
            desc = BeautifulSoup(f.get("description", ""), "html.parser").get_text(" ", strip=True)
            out.append(SimpleNamespace(title=clean_title(f.get("title")),
                                       link=f.get("link", "").strip(), desc=desc))
    except Exception as ex:
        print("RSSを読めません:", feed, ex)
    return [e for e in out if e.title and not broken(e.title)]


def from_gnews(url):
    domain = urlparse(url).hostname.replace("www.", "")
    span = "1d" if SLOT == "am" else "12h"
    feed = f"https://news.google.com/rss/search?q={quote('site:' + domain + ' when:' + span)}&hl=ja&gl=JP&ceid=JP:ja"
    out = rss_items(feed)
    print(f"Googleニュースの見出し {len(out)}件:", domain)
    return [SimpleNamespace(title=e.title, link="", desc="") for e in out]


def from_top(url, pattern):
    if not allowed(url):
        return []
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
            out.append(SimpleNamespace(title=a.get_text(" ", strip=True), link=h, desc=""))
    print(f"記事リンク {len(out)}件:", url)
    return out


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


READING = {
    "北日本新聞": "きたにっぽん新聞",
    "北日本放送": "きたにほん放送",
    "北國新聞": "ほっこく新聞",
}


def speak(script, path):
    for k, v in READING.items():
        script = script.replace(k, v)
    for _ in range(3):
        try:
            asyncio.run(edge_tts.Communicate(script, VOICE).save(str(path)))
            if path.exists() and path.stat().st_size > 1000:
                return True
        except Exception as ex:
            print("読み上げ失敗:", str(ex)[:300])
        time.sleep(30)
    return False


def make(n, name, group, link, cands):
    """新聞1紙分のまとめ音声を作る"""
    global items
    if any(x["site"] == name and x["date"] == TODAY.isoformat() and x.get("slot") == SLOT for x in items):
        print(f"作成済み（{WHEN}）:", name)
        return
    news, heads = [], []
    for e in cands[:25]:
        if len(news) >= NEWS_PER_PAPER:
            break
        title, text = read_article(e.link) if e.link else ("", "")
        title = title or clean_title(e.title)
        if not title or title in used or title in heads:
            continue
        text = text or e.desc
        heads.append(title)
        news.append(f"・見出し：{title}" + (f"\n　内容：{text}" if text else ""))
    print(f"{name}（{WHEN}）：ニュース {len(news)}本")
    if len(news) < 3:
        print("ニュースが少ないのでスキップ:", name)
        return
    prompt = (
        f"あなたはラジオのニュース解説者です。以下は、{name}の今日の主なニュースの見出しと、無料で読める範囲の内容です。"
        "内容がないものは見出しから分かる範囲で話してください。"
        f"これをもとに、「ながら日経」のように、耳で聞いて分かる約5分（1400〜1600字）の{WHEN}のニュース解説原稿を作ってください。"
        f"最初に「{TODAY.month}月{TODAY.day}日、{name}の{WHEN}のニュースをお伝えします」と始め、"
        "重要そうな順に、何が起きたか、ポイントは何か、の流れでかいつまんで話してください。"
        "書かれていないことは、推測で付け足さないでください。最後にひとことでまとめてください。"
        "見出しや記号は使わず話し言葉で、原稿の本文だけを出力してください。\n\n" + "\n".join(news)
    )
    script = write_script(prompt)
    if not script:
        print("原稿を作れずスキップ:", name)
        return
    fname = "news_" + NOW.strftime("%Y%m%d%H%M%S") + f"_{group}{n}"
    mp3 = audio_dir / f"{fname}.mp3"
    if not speak(script, mp3):
        print("音声を作れずスキップ:", name)
        return
    # 前の日の分と、同じ回の古い分は片付ける
    old = [x for x in items if x["site"] == name and (x["date"] != TODAY.isoformat() or x.get("slot", "am") == SLOT)]
    for x in old:
        f = root / x["file"]
        if f.exists():
            f.unlink()
    items = [x for x in items if x not in old]
    items.append({"title": f"{WHEN}のニュースまとめ", "site": name, "group": group, "slot": SLOT,
                  "count": len(news), "link": link, "file": f"audio/{fname}.mp3", "date": TODAY.isoformat()})
    used.extend(heads)
    save()
    print(f"完成: {WHEN}のまとめ", name)


for n, (name, url, pattern) in enumerate(PAPERS):
    try:
        cands = from_top(url, pattern)
        if len(cands) < 3:
            cands = from_gnews(url)
        make(n, name, "paper", url, cands)
    except Exception as ex:
        print("まとめを作れません:", name, ex)

for n, (name, mid) in enumerate(LOCALS):
    try:
        cands = rss_items(f"https://news.yahoo.co.jp/rss/media/{mid}/all.xml")
        print(f"Yahoo!ニュースの記事 {len(cands)}件:", name)
        make(n, name, "local", f"https://news.yahoo.co.jp/media/{mid}", cands)
    except Exception as ex:
        print("まとめを作れません:", name, ex)

for n, (name, url, feed) in enumerate(BIZ):
    try:
        cands = rss_items(feed) if feed else []
        print(f"RSSの記事 {len(cands)}件:", name)
        if len(cands) < 3:
            cands = from_gnews(url)
        make(n, name, "biz", url, cands)
    except Exception as ex:
        print("まとめを作れません:", name, ex)

order = [p[0] for p in PAPERS] + [p[0] for p in LOCALS] + [p[0] for p in BIZ]
items.sort(key=lambda x: (x.get("slot") != "pm", order.index(x["site"]) if x["site"] in order else 99))
save()
