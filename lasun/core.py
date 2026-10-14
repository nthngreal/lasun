"""Ядро Ласуна: збір товарів з atbmarket.com, калорії/цукор, оцінка й порції.

Без Qt, лише стандартна бібліотека: так його можна тестувати окремо.
"""
import datetime as dt
import gzip
import html as htmlmod
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor

SITE = "https://www.atbmarket.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
OFF_UA = "Lasun/0.1 (personal ATB snack helper; github.com/nthngreal/lasun)"

log = logging.getLogger("lasun")

# Групи → розділи каталогу АТБ. Порядок важливий: товар іде в першу групу.
# (ключ, чи це напої, розділи)
GROUPS = [
    ("drinks", True, ["307-napoi", "364-yenergetichni", "kvas", "324-soki-nektari"]),
    ("dairy", False, ["349-yogurti", "382-molochni-deserti", "317-sirki-glazurovani",
                      "379-sir-kislomolochniy", "316-kislomolochni-napoi"]),
    ("icecream", False, ["334-morozivo"]),
    ("sweets", False, ["371-diabetichna-ta-dietichna-produktsiya", "327-batonchiki",
                       "kruasani-ta-zefir", "303-shokolad", "pecivo-praniki", "338-vafli",
                       "321-tsukerki", "biskviti-ta-deserti", "300-skhidni-solodoshchi"]),
    ("snacks", False, ["sneki", "cipsi", "409-krekeri", "347-khlibtsi",
                       "446-gorikhi-sukhofrukti"]),
]
CATEGORY_COUNT = sum(len(g[2]) for g in GROUPS)

# Пороги на 100 г (для напоїв — на 100 мл). Цукор — за світлофором FSA
# (мало ≤5 г, для напоїв ≤2,5 г); калорії — поріг «легкого» перекусу.
#           зелений (ккал, цукор)  жовтий (ккал, цукор)
TIERS = {
    "food":  ((150, 5.0), (250, 15.0)),
    "drink": ((20, 2.5), (45, 6.0)),
}
GREEN, YELLOW, RED, UNKNOWN = 0, 1, 2, 9

DEFAULTS = {"daily_kcal": 1800, "snack_kcal": 250, "sugar_g": 30}


class LasunError(Exception):
    """Помилка з кодом рядка для інтерфейсу (i18n)."""
    def __init__(self, code, detail=""):
        super().__init__(detail or code)
        self.code, self.detail = code, detail


class Blocked(LasunError):
    pass


class Cancelled(Exception):
    pass


# ---------- HTTP ----------

def http_get(url, timeout=30, headers=None, tries=3):
    h = {"User-Agent": UA, "Accept-Language": "uk-UA,uk;q=0.9",
         "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
         "Accept-Encoding": "gzip, deflate"}
    h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                enc = r.headers.get("Content-Encoding", "")
            if enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw)
            return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read()[:3000].decode("utf-8", "replace")
            except Exception:
                pass
            if e.code == 404:
                return None
            if e.code in (403, 429, 503) and ("cloudflare" in body.lower()
                                              or "just a moment" in body.lower()
                                              or "cf-" in body.lower()):
                last = Blocked("err_blocked", "HTTP %s" % e.code)
            else:
                last = LasunError("err_http", "HTTP %s: %s" % (e.code, url))
        except Exception as e:
            last = LasunError("err_network", str(e))
        time.sleep(3 * (i + 1))
    raise last


# ---------- розбір сторінок АТБ ----------

def to_text(html):
    html = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d|span|td|dd|dt)>", "\n", html)
    text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", html))
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def num(s):
    try:
        return float(s.replace(",", ".").rstrip("."))
    except (ValueError, AttributeError):
        return None


_PROD_RE = re.compile(r'href="(?:https?://(?:www\.)?atbmarket\.com)?/product/([^"?#/\s]+)"')


def _card_starts(html, firsts):
    """Шукає повторюваний контейнер картки: клас, що стоїть рівно перед кожним
    першим посиланням на товар (між ним і попереднім). Повертає позиції початку."""
    n = len(firsts)
    if n < 2:
        return None
    pos_by_class = {}
    for m in re.finditer(r'<(?:div|article|li|section)\b[^>]*\bclass="([^"]+)"', html):
        pos_by_class.setdefault(m.group(1), []).append(m.start())
    best = None
    link_pos = [p for p, _ in firsts]
    for cls, ps in pos_by_class.items():
        if not (n - 1 <= len(ps) <= n + 2):
            continue
        starts, j = [], 0
        for i, p in enumerate(link_pos):
            lo = link_pos[i - 1] if i else -1
            cand = [q for q in ps if lo < q <= p]
            if not cand:
                break
            starts.append(cand[0])
        if len(starts) != n:
            continue
        spread = sum(p - q for p, q in zip(link_pos, starts))  # більше = зовнішніший
        if best is None or spread > best[0]:
            best = (spread, starts)
    return best[1] if best else None


def split_cards(html):
    """[(slug, html шматок картки)] у порядку на сторінці."""
    links = [(m.start(), m.group(1)) for m in _PROD_RE.finditer(html)]
    firsts, seen = [], set()
    for pos, slug in links:
        if slug not in seen:
            seen.add(slug)
            firsts.append((pos, slug))
    if not firsts:
        return []
    starts = _card_starts(html, firsts)
    out = []
    for i, (pos, slug) in enumerate(firsts):
        if starts:
            a = starts[i]
            b = starts[i + 1] if i + 1 < len(starts) else min(len(html), pos + 6000)
        else:
            # запасний варіант: від попереднього товару до наступного
            a = firsts[i - 1][0] + 1 if i else max(0, pos - 600)
            b = firsts[i + 1][0] if i + 1 < len(firsts) else min(len(html), pos + 6000)
        out.append((slug, html[a:b]))
    return out


def parse_card(slug, chunk):
    text = to_text(chunk)
    until = None
    m = re.search(r"до\s*(\d{2}\.\d{2})(?:\.\d{2,4})?", text)
    if m:
        until = m.group(1)
        text_np = text[:m.start()] + " " + text[m.end():]
    else:
        text_np = text
    prices = [num(x) for x in re.findall(
        r"(?<![\d,.])(\d{1,6}\.\d{2})(?![\d])(?!\s*(?:(?:л|мл|г|гр|кг)(?![а-яіїєґ])|%))", text_np)]
    # ціни також часто лежать у value="" / data-price=""
    for x in re.findall(r'(?:value|data-price|content)="(\d{1,6}\.\d{2})"', chunk):
        prices.append(num(x))
    prices = [p for p in prices if p and p > 0]
    disc = None
    m = re.search(r"(?<![\w,.])[-−–]\s?(\d{1,2})\s?%", text)
    if m:
        disc = int(m.group(1))
    price = old = None
    if prices:
        uniq = sorted(set(prices))
        price = uniq[0]
        if len(uniq) > 1 and (disc or uniq[-1] > price * 1.05):
            old = uniq[-1]
            # якщо знайшли знижку — беремо найближчу до неї пару цін
            if disc:
                best = min(((abs((1 - a / b) * 100 - disc), a, b)
                            for a in uniq for b in uniq if b > a), default=None)
                if best and best[0] < 4:
                    price, old = best[1], best[2]
    if old and not disc:
        disc = int(round((1 - price / old) * 100))
    if not old:
        disc = None
    name = None
    for pat in (r'alt="([^"]{5,200})"', r'title="([^"]{5,200})"'):
        m = re.search(pat, chunk)
        if m:
            name = htmlmod.unescape(m.group(1)).strip()
            break
    if not name:
        lines = [ln for ln in text.split("\n") if re.search(r"[А-Яа-яІіЇїЄєҐґ]{3}", ln)]
        name = max(lines, key=len)[:200] if lines else slug
    m = re.search(r"photos/(\d+)/", chunk)
    img = ("https://src.zakaz.atbmarket.com/cache/photos/%s/catalog_list_%s.jpg"
           % (m.group(1), m.group(1))) if m else None
    unit = "кг" if re.search(r"грн\s*/\s*кг", text) else "шт"
    return {"slug": slug, "name": name, "price": price, "old_price": old,
            "discount": disc, "promo_until": until if old else None,
            "img": img, "unit": unit}


def parse_weight(name):
    """(вага в г або мл, рідина?) з назви «Йогурт 280г», «Сік 0,95л», «4х100г»."""
    s = (name or "").lower().replace(" ", " ")
    m = re.search(r"(\d+)\s*[xх×*]\s*(\d+(?:[.,]\d+)?)\s*(г|гр|мл|кг|л)\b", s)
    mult = 1
    if m:
        mult = int(m.group(1))
        val, u = num(m.group(2)), m.group(3)
    else:
        m = re.search(r"(?<![\d,.])(\d+(?:[.,]\d+)?)\s*(г|гр|мл|кг|л)(?![а-яіїєґ])", s)
        if not m:
            return None, False
        val, u = num(m.group(1)), m.group(2)
    if val is None:
        return None, False
    if u in ("кг", "л"):
        val *= 1000
    return val * mult, u in ("мл", "л")


_NUTRI_START = re.compile(
    r"(?i)(харчова|поживна|енергетична|калорійн|ккал|кдж|в\s*100\s*(?:г|мл)|білк)")


def parse_nutrition(text):
    """ккал, цукор, білки, жири на 100 г з вільного тексту опису."""
    out = {}
    starts = [m.start() for m in _NUTRI_START.finditer(text)]
    if not starts:
        return out
    a = max(0, starts[0] - 200)
    win = text[a:starts[-1] + 400]
    low = win.lower()
    m = (re.search(r"(\d+(?:[.,]\d+)?)\s*к\s?кал", low)
         or re.search(r"к\s?кал\W{0,12}(\d+(?:[.,]\d+)?)", low))
    if m:
        out["kcal"] = num(m.group(1))
    else:
        m = (re.search(r"(\d+(?:[.,]\d+)?)\s*кдж", low)
             or re.search(r"кдж\W{0,12}(\d+(?:[.,]\d+)?)", low))
        if m and num(m.group(1)):
            out["kcal"] = round(num(m.group(1)) / 4.184, 1)
    for key, pat in (("sugar", r"цукр(?:и|ів|ах)?\b"), ("protein", r"білк\w*"),
                     ("fat", r"жир\w*(?:\s*загалом)?")):
        m = re.search(pat + r"\W{0,20}?(\d+(?:[.,]\d+)?)\s*г", low)
        if not m:
            m = re.search(pat + r"\W{0,20}?(\d+(?:[.,]\d+)?)", low)
        if m:
            out[key] = num(m.group(1))
    if "sugar" not in out and re.search(r"без\s+(?:доданого\s+)?цукру", low) \
            and "kcal" in out:
        out["sugar_free_claim"] = True
    # Відкидаємо явні дурниці (напр., вагу упаковки, прочитану як ккал).
    if out.get("kcal") is not None and not (0 <= out["kcal"] <= 950):
        out.pop("kcal")
    if out.get("sugar") is not None and not (0 <= out["sugar"] <= 100):
        out.pop("sugar")
    return out


def parse_product_page(html):
    text = to_text(html)
    name = None
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html)
    if m:
        name = to_text(m.group(1)).strip()
    if not name:
        m = re.search(r"(?is)<title>(.*?)</title>", html)
        if m:
            name = re.sub(r"\s+купити.*$", "", htmlmod.unescape(m.group(1))).lstrip("➤ ").strip()
    nut = parse_nutrition(text)
    weight, liquid = parse_weight(name or "")
    if not weight:
        m = re.search(r"(?i)(?:Вага|Об'?єм|Обʼєм|Об’єм)\s*:?\s*(\d+(?:[.,]\d+)?)\s*(г|гр|кг|мл|л)\b", text)
        if m:
            weight, liquid = parse_weight(m.group(1) + m.group(2))
    return {"name": name, "weight": weight, "liquid": liquid, **nut}


# ---------- Open Food Facts ----------

def _tokens(s):
    s = re.sub(r"\d+(?:[.,]\d+)?\s*(?:г|гр|кг|мл|л|%)?", " ", (s or "").lower())
    return [t[:6] for t in re.findall(r"[a-zа-яіїєґ']{3,}", s)
            if t not in ("для", "без", "with", "the", "and", "стакан", "пачка")]


def off_lookup(name):
    q = " ".join(re.findall(r"[A-Za-zА-Яа-яІіЇїЄєҐґ']{3,}", re.sub(r"\d\S*", " ", name)))[:120]
    if not q:
        return None
    url = ("https://world.openfoodfacts.org/cgi/search.pl?" + urllib.parse.urlencode({
        "search_terms": q, "search_simple": 1, "action": "process", "json": 1,
        "page_size": 12, "fields": "code,product_name,product_name_uk,brands,"
                                   "nutriments,nutriscore_grade"}))
    raw = http_get(url, headers={"User-Agent": OFF_UA, "Accept": "application/json"},
                   timeout=40, tries=2)
    if not raw:
        return None
    want = set(_tokens(name))
    if len(want) < 2:
        return None
    best = None
    for p in json.loads(raw).get("products", []):
        n = p.get("nutriments") or {}
        kcal = n.get("energy-kcal_100g")
        if kcal is None and n.get("energy_100g") is not None:
            kcal = float(n["energy_100g"]) / 4.184
        if kcal is None:
            continue
        have = set(_tokens(" ".join([p.get("product_name_uk") or "",
                                     p.get("product_name") or "", p.get("brands") or ""])))
        hit = len(want & have)
        score = hit / len(want)
        if hit >= 2 and score >= 0.7 and (not best or score > best[0]):
            best = (score, {"kcal": round(float(kcal), 1),
                            "sugar": n.get("sugars_100g"),
                            "protein": n.get("proteins_100g"),
                            "fat": n.get("fat_100g"),
                            "nutriscore": (p.get("nutriscore_grade") or "").upper()[:1] or None})
    return best[1] if best else None


# ---------- база ----------

class Store:
    """SQLite у теці даних користувача. Один об'єкт на потік."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS products (
      id INTEGER PRIMARY KEY, slug TEXT UNIQUE, name TEXT, grp TEXT, cat TEXT,
      price REAL, old_price REAL, discount INTEGER, promo_until TEXT,
      unit TEXT, img TEXT, weight REAL, liquid INTEGER DEFAULT 0,
      kcal REAL, sugar REAL, protein REAL, fat REAL, src TEXT, nutriscore TEXT,
      detail_ts REAL, off_ts REAL, seen_ts REAL);
    CREATE TABLE IF NOT EXISTS eaten (
      id INTEGER PRIMARY KEY, day TEXT, pid INTEGER, name TEXT,
      grams REAL, kcal REAL, sugar REAL, ts REAL);
    CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
    """

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.con = sqlite3.connect(path, timeout=30)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(self.SCHEMA)

    def get(self, k, default=None):
        r = self.con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set(self, k, v):
        self.con.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (k, str(v)))
        self.con.commit()

    def last_crawl(self):
        return float(self.get("last_crawl", 0) or 0)

    def counts(self):
        r = self.con.execute("SELECT COUNT(*), SUM(kcal IS NOT NULL) FROM products").fetchone()
        return r[0] or 0, r[1] or 0

    # --- товари для вигляду ---
    def products(self, view, only_good=False, search=""):
        """(товари з калоріями — відсортовані, кількість без даних).
        view: ключ групи або "promo". Спершу акції серед 🟢🟡, далі найлегше."""
        last = self.last_crawl()
        rows = [dict(r) for r in self.con.execute(
            "SELECT * FROM products WHERE seen_ts>=? AND price IS NOT NULL",
            (last - 3 * 86400 if last else 0,))]
        if view == "promo":
            rows = [r for r in rows if r["old_price"]]
        else:
            rows = [r for r in rows if r["grp"] == view]
        if search:
            words = search.lower().split()
            rows = [r for r in rows if all(w in (r["name"] or "").lower() for w in words)]
        unknown = sum(1 for r in rows if r["kcal"] is None)
        rows = [r for r in rows if r["kcal"] is not None]
        if only_good or view == "promo":
            rows = [r for r in rows if tier(r) <= YELLOW]

        def key(r):
            t = tier(r)
            bucket = 0 if r["old_price"] and t <= YELLOW else 1 if t <= YELLOW else 2
            return (bucket, t, -(r["discount"] or 0), r["kcal"])
        rows.sort(key=key)
        return rows, unknown

    def product(self, pid):
        r = self.con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None

    # --- журнал ---
    def eat(self, pid, name, grams, kcal, sugar, day=None):
        self.con.execute("INSERT INTO eaten (day,pid,name,grams,kcal,sugar,ts) VALUES "
                         "(?,?,?,?,?,?,?)", (day or today(), pid, name, grams, kcal, sugar,
                                             time.time()))
        self.con.commit()

    def eat_product(self, p, grams):
        self.eat(p["id"], p["name"], grams, p["kcal"] * grams / 100,
                 (p["sugar"] or 0) * grams / 100)

    def eaten(self, day=None):
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM eaten WHERE day=? ORDER BY ts", (day or today(),))]

    def totals(self, day=None):
        r = self.con.execute("SELECT COALESCE(SUM(kcal),0), COALESCE(SUM(sugar),0) "
                             "FROM eaten WHERE day=?", (day or today(),)).fetchone()
        return r[0], r[1]

    def undo(self, eid):
        self.con.execute("DELETE FROM eaten WHERE id=?", (eid,))
        self.con.commit()


def today():
    return dt.date.today().isoformat()


# ---------- збір ----------

def crawl(path, progress=None, cancel=None, workers=4, max_off=300):
    """Повний обхід: списки (ціни, акції) → сторінки товарів (склад) → Open Food Facts.

    progress(stage, done, total) викликається з цього потоку; stage: list | detail | off.
    cancel — threading.Event; повертає кількість побачених товарів."""
    cancel = cancel or threading.Event()
    progress = progress or (lambda *a: None)
    st = Store(path)
    t0 = time.time()

    def check():
        if cancel.is_set():
            raise Cancelled()

    def pause(sec):
        if cancel.wait(sec):
            raise Cancelled()

    seen = 0
    done_cats = 0
    for gkey, is_drink, cats in GROUPS:
        for cat in cats:
            check()
            progress("list", done_cats, CATEGORY_COUNT)
            slugs = set()
            for page in range(1, 40):
                url = "%s/catalog/%s%s" % (SITE, cat, "?page=%d" % page if page > 1 else "")
                html = http_get(url)
                pause(0.8 + random.random() * 0.6)
                if not html:
                    break
                new = [(s, c) for s, c in split_cards(html) if s not in slugs]
                if not new:
                    break
                for slug, chunk in new:
                    slugs.add(slug)
                    d = parse_card(slug, chunk)
                    w, liq = parse_weight(d["name"])
                    st.con.execute("""
                      INSERT INTO products (slug,name,grp,cat,price,old_price,discount,
                        promo_until,unit,img,weight,liquid,seen_ts)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(slug) DO UPDATE SET price=excluded.price,
                        old_price=excluded.old_price, discount=excluded.discount,
                        promo_until=excluded.promo_until, unit=excluded.unit,
                        img=COALESCE(excluded.img, img), seen_ts=excluded.seen_ts,
                        weight=COALESCE(weight, excluded.weight)""",
                                   (slug, d["name"], gkey, cat, d["price"], d["old_price"],
                                    d["discount"], d["promo_until"], d["unit"], d["img"],
                                    w, int(bool(liq or is_drink)), t0))
                st.con.commit()
            log.info("Розділ %s: %d", cat, len(slugs))
            seen += len(slugs)
            done_cats += 1
    if seen == 0:
        raise LasunError("err_site_changed")
    # Ціни й акції вже свіжі: показуємо їх, поки дочитуються склади.
    st.set("last_crawl", t0)
    progress("list", CATEGORY_COUNT, CATEGORY_COUNT)

    # Сторінки товарів: склад не змінюється, тож раз на 30 днів.
    rows = [dict(r) for r in st.con.execute(
        """SELECT id, slug FROM products WHERE seen_ts>=?
           AND (detail_ts IS NULL OR detail_ts<?)""", (t0, t0 - 30 * 86400))]
    lock = threading.Lock()
    done = [0]
    blocked = []

    def one(r):
        if cancel.is_set() or blocked:
            return None
        try:
            html = http_get("%s/product/%s" % (SITE, r["slug"]))
        except Blocked as e:
            blocked.append(e)
            return None
        except Exception as e:
            log.warning("Товар %s: %s", r["slug"], e)
            html = None
        cancel.wait(0.4 + random.random() * 0.5)
        return r["id"], parse_product_page(html) if html else None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(one, rows):
            with lock:
                done[0] += 1
            if res and res[1] is not None:
                pid, d = res
                st.con.execute("""UPDATE products SET name=COALESCE(?,name),
                    weight=COALESCE(?,weight), liquid=MAX(liquid,?),
                    kcal=?, sugar=?, protein=?, fat=?, src=?, detail_ts=? WHERE id=?""",
                               (d.get("name"), d.get("weight"), int(bool(d.get("liquid"))),
                                d.get("kcal"), 0.0 if d.get("sugar_free_claim") else d.get("sugar"),
                                d.get("protein"), d.get("fat"),
                                "atb" if d.get("kcal") is not None else None, time.time(), pid))
                if done[0] % 20 == 0:
                    st.con.commit()
            if done[0] % 10 == 0 or done[0] == len(rows):
                progress("detail", done[0], len(rows))
    st.con.commit()
    if blocked:
        raise blocked[0]
    check()

    # Open Food Facts — для тих, де в АТБ калорій нема (їхній ліміт ~10 пошуків/хв).
    rows = [dict(r) for r in st.con.execute(
        """SELECT id, name FROM products WHERE seen_ts>=? AND kcal IS NULL
           AND detail_ts IS NOT NULL AND (off_ts IS NULL OR off_ts<?) LIMIT ?""",
        (t0, t0 - 30 * 86400, max_off))]
    for i, r in enumerate(rows):
        check()
        progress("off", i, len(rows))
        try:
            d = off_lookup(r["name"])
        except Exception as e:
            log.warning("OFF %s: %s", r["name"], e)
            d = None
        if d:
            st.con.execute("""UPDATE products SET kcal=?, sugar=?, protein=?, fat=?,
                nutriscore=?, src='off', off_ts=? WHERE id=?""",
                           (d["kcal"], d["sugar"], d["protein"], d["fat"], d["nutriscore"],
                            time.time(), r["id"]))
        else:
            st.con.execute("UPDATE products SET off_ts=? WHERE id=?", (time.time(), r["id"]))
        st.con.commit()
        pause(6.5)
    st.set("last_crawl_ok", time.time())
    progress("done", 1, 1)
    return seen


# ---------- оцінка й порції ----------

def tier(p):
    if p["kcal"] is None:
        return UNKNOWN
    sugar = p["sugar"] if p["sugar"] is not None else 99
    (gk, gs), (yk, ys) = TIERS["drink" if p["liquid"] else "food"]
    if p["kcal"] <= gk and sugar <= gs:
        return GREEN
    if p["kcal"] <= yk and sugar <= ys:
        return YELLOW
    return RED


def portion(p, kcal_left, sugar_left, snack_kcal):
    """Скільки можна з'їсти зараз. Повертає dict:
    kind: unknown | day_full | sugar_full | free | packs | whole | part
    grams (на кнопку «З'їв»), packs, kcal, frac (частка упаковки), by_sugar."""
    if p["kcal"] is None:
        return {"kind": "unknown", "grams": None}
    if p["kcal"] < 5 and (p["sugar"] or 0) < 0.5:
        return {"kind": "free", "grams": p["weight"] or 250}
    budget = min(float(snack_kcal), float(kcal_left))
    if budget < 40:
        return {"kind": "day_full", "grams": None}
    lim = budget / p["kcal"] * 100
    by_sugar = False
    if p["sugar"] and p["sugar"] > 0:
        s_lim = max(float(sugar_left), 0) / p["sugar"] * 100
        if s_lim < lim:
            lim, by_sugar = s_lim, True
    lim = int(lim // 10 * 10)
    if lim < 20:
        return {"kind": "sugar_full" if by_sugar else "day_full", "grams": None}
    w = p["weight"]
    if w and lim >= w:
        packs = min(int(lim // w), 4)
        return {"kind": "packs" if packs >= 2 else "whole", "grams": w, "packs": packs,
                "kcal": p["kcal"] * w / 100, "by_sugar": by_sugar}
    return {"kind": "part", "grams": lim, "kcal": p["kcal"] * lim / 100,
            "frac": lim / w if w else None, "by_sugar": by_sugar}


def frac_symbol(frac):
    return ("¼" if frac < 0.37 else "⅓" if frac < 0.45 else "½" if frac < 0.62
            else "⅔" if frac < 0.8 else "¾")


# ---------- перевірка сайту (для кнопки «Перевірити» і CI) ----------

def check_site():
    """Повертає рядки звіту: 1 розділ + 2 товари."""
    out = []
    html = http_get(SITE + "/catalog/349-yogurti") or ""
    cards = split_cards(html)
    out.append("cards: %d" % len(cards))
    for slug, chunk in cards[:3]:
        d = parse_card(slug, chunk)
        out.append("  %s | %s | old %s | -%s%% %s" % (d["name"][:50], d["price"], d["old_price"],
                                                     d["discount"], d["promo_until"]))
    for slug, _ in cards[:2]:
        time.sleep(1)
        d = parse_product_page(http_get("%s/product/%s" % (SITE, slug)) or "")
        out.append("  product %s: kcal=%s sugar=%s weight=%s" % (
            (d.get("name") or slug)[:40], d.get("kcal"), d.get("sugar"), d.get("weight")))
    return out, html
