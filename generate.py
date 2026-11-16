import os, re, sys, json, time, asyncio, subprocess, datetime, pathlib
from types import SimpleNamespace
try:
    import edge_tts
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "edge-tts"], check=True)
    import edge_tts
import requests, feedparser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from bs4 import BeautifulSoup
from google import genai

# 読みたいサイトの一覧（増やすときはここに1行足す）
# 3つ目は「RSSがないサイト」用の、記事URLの形。RSSがあるサイトは "" のままでOK
SITES = [
    ("民放online", "https://minpo.online/", ""),
    ("POTLUCK YAESU", "https://www.potluck-yaesu.com/", r"/magazine/\d{8}/\d+/"),
    ("ソトコト", "https://sotokoto-online.jp/", ""),
    ("greenz", "https://greenz.jp/", ""),
    ("LOCAL LETTER", "https://localletter.jp/", ""),
    ("TOMORUBA", "https://tomoruba.eiicon.net/", ""),
    ("BRIDGE", "https://thebridge.jp/", ""),
    ("The H Magazine", "https://www.hakuhodo.co.jp/magazine/", r"/magazine/\d+/?$"),
    ("生活者DDM通信", "https://seikatsusha-ddm.com/", r"/article/\d+/?$"),
    ("BUSINESS INSIDER", "https://www.businessinsider.jp/", ""),
]
MAX_PER_SITE = 1   # 1サイトあたり1回に作る最大本数
MAX_TOTAL = 4      # 全体で1回に作る最大本数
TEXT_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
VOICE = "ja-JP-NanamiNeural"
UA = {"User-Agent": "Mozilla/5.0"}
READING = {
    "北日本新聞": "きたにっぽん新聞",
    "北日本放送": "きたにほん放送",
    "北國新聞": "ほっこく新聞",
}

JST = datetime.timezone(datetime.timedelta(hours=9))
NOW = datetime.datetime.now(JST)
TODAY = NOW.date()

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
robots = {}


def broken(t):
    """文字化けしたタイトルかどうか"""
    return bool(re.search("[à-ï][\u0080-¿]{2}", t or ""))


# 文字化けしてしまった解説は消して、作り直す
for x in [x for x in eps if broken(x["title"])]:
    f = root / x["file"]
    if f.exists():
        f.unlink()
    seen.discard(x["link"])
    print("文字化けを作り直し:", x["link"])
eps = [x for x in eps if not broken(x["title"])]

# 「聞いた」を押した解説を消す
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
    (root / "sites.json").write_text(json.dumps([s[0] for s in SITES], ensure_ascii=False), encoding="utf-8")


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


def soup(url):
    return BeautifulSoup(requests.get(url, headers=UA, timeout=30).content, "html.parser")


def clean_title(t):
    return re.sub(r"(?:\s[-–]\s|\s*[|｜]\s*)[^-–|｜]+$", "", t or "").strip()


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


def from_page(url, pattern):
    """RSSがないサイト：トップページから記事のリンクを集める"""
    items, got = [], set()
    if not allowed(url):
        return items
    try:
        s = soup(url)
    except Exception as ex:
        print("サイトを開けません:", url, ex)
        return items
    for a in s.find_all("a", href=True):
        h = urljoin(url, a["href"]).split("#")[0]
        if re.search(pattern, h) and h not in got:
            got.add(h)
            items.append(SimpleNamespace(link=h, title=a.get_text(" ", strip=True), text=""))
    print(f"記事リンク {len(items)}件:", url)
    return items


def from_feed(url):
    cands = []
    try:
        s = soup(url)
        for l in s.find_all("link", type=re.compile("rss|atom")):
            cands.append(urljoin(url, l["href"]))
    except Exception as ex:
        print("サイトを開けません:", url, ex)
    cands += [urljoin(url, "feed/"), urljoin(url, "rss"), urljoin(url, "feed")]
    for c in cands:
        try:
            f = feedparser.parse(requests.get(c, headers=UA, timeout=30).content)
            if f.entries:
                print(f"RSS {len(f.entries)}件:", c)
                out = []
                for e in f.entries:
                    if not e.get("link"):
                        continue
                    body = e.get("content", [{}])[0].get("value", "") or e.get("summary", "")
                    text = BeautifulSoup(body, "html.parser").get_text("\n", strip=True)
                    out.append(SimpleNamespace(link=e.link, title=e.get("title", ""), text=text))
                return out
        except Exception:
            pass
    print("RSSが見つかりませんでした:", url)
    return []


def read_article(e):
    """記事の本文を取る。読み取りが許可されていなければ、RSSの中身だけを使う"""
    title, text = clean_title(e.title), e.text
    if allowed(e.link):
        try:
            page = soup(e.link)
            og = page.find("meta", property="og:title")
            if og and og.get("content") and not broken(og["content"]):
                title = clean_title(og["content"])
            for t in page(["script", "style", "nav", "header", "footer", "aside"]):
                t.decompose()
            body = (page.find("article") or page.find("main") or page.body).get_text("\n", strip=True)
            if len(body) > len(text):
                text = body
        except Exception as ex:
            print("記事を開けません:", e.link, ex)
    return title, text[:20000]


# サイトごとに新着を集める（初めてのサイトは最新1本だけにして、古い記事でいっぱいにならないようにする）
per_site = []
for name, url, pattern in SITES:
    items = from_page(url, pattern) if pattern else from_feed(url)
    key = "init:" + name
    if key not in seen and items:
        for e in items[1:]:
            seen.add(e.link)
        seen.add(key)
        print("初めてのサイトなので最新1本だけ:", name)
    new = [e for e in items if e.link not in seen][:MAX_PER_SITE]
    per_site.append([(name, e) for e in new])

# 1サイトずつ順番に選んで、全体の上限までにする
todo = []
while len(todo) < MAX_TOTAL and any(per_site):
    for lst in per_site:
        if lst and len(todo) < MAX_TOTAL:
            todo.append(lst.pop(0))

for i, (name, e) in enumerate(reversed(todo)):
    try:
        title, text = read_article(e)
        if re.search(r"申込|申し込み|フォーム|お問い合わせ|メールマガジン登録", title):
            print("記事ではないのでスキップ:", title)
            seen.add(e.link)
            continue
        if len(text) < 200:
            print("本文が短すぎるのでスキップ:", title)
            seen.add(e.link)
            continue
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
        fname = NOW.strftime("%Y%m%d%H%M%S") + f"_{i}"
        mp3 = audio_dir / f"{fname}.mp3"
        if not speak(script, mp3):
            print("音声を作れずスキップ:", title)
            continue
        eps.insert(0, {"title": f"{title} - {name}", "site": name, "link": e.link,
                       "file": f"audio/{fname}.mp3", "date": TODAY.isoformat()})
        seen.add(e.link)
        save()
        print("完成:", name, title)
    except Exception as ex:
        print("スキップ:", e.link, ex)

save()
