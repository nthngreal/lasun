"""Вікно Ласуна: розділи зліва, картки товарів посередині, «Сьогодні» справа.

Товари збирає core.crawl у фоновому потоці; вікно читає їх зі своєї SQLite."""
import datetime as dt
import html
import logging
import os
import sys
import threading
import time

from PySide6.QtCore import QSettings, QSize, QStandardPaths, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkRequest
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QSpinBox, QVBoxLayout, QWidget)

from . import core, desktop, theme
from .i18n import LANGS, STRINGS

NAV = ["promo"] + [g[0] for g in core.GROUPS]
NAV_ICON = {"promo": "🔥", "drinks": "🥤", "dairy": "🥛", "icecream": "🍦", "sweets": "🍫",
            "snacks": "🍿"}
PAGE = 48
CARD_MIN_W = 230
GAP = 14
STALE_SEC = 18 * 3600   # старіше — оновлюємо самі при запуску

log = logging.getLogger("lasun")


class _Args(dict):
    def __missing__(self, key):
        return "?"


def label(text="", name=None, wrap=False):
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl


def fnum(x, lang="en"):
    if x is None:
        return "?"
    s = ("%.1f" % x).rstrip("0").rstrip(".")
    return s if lang == "en" else s.replace(".", ",")


def clear_layout(lay):
    while lay.count():
        w = lay.takeAt(0).widget()
        if w:
            w.hide()
            w.deleteLater()


def data_dir():
    d = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    os.makedirs(d, exist_ok=True)
    return d


class Crawler(QThread):
    progress = Signal(str, int, int)
    failed = Signal(str, str)
    done = Signal(int)

    def __init__(self, db_path):
        super().__init__()
        self.db_path = db_path
        self.cancel = threading.Event()

    def run(self):
        try:
            n = core.crawl(self.db_path, progress=lambda *a: self.progress.emit(*a),
                           cancel=self.cancel)
            self.done.emit(n)
        except core.Cancelled:
            self.failed.emit("cancelled", "")
        except core.LasunError as e:
            log.warning("crawl: %s %s", e.code, e.detail)
            self.failed.emit(e.code, e.detail)
        except Exception as e:  # noqa: BLE001 — показати людині, а не впасти
            log.exception("crawl")
            self.failed.emit("err_other", str(e))


class Images:
    """Фото товарів з диск-кешем; кожен QLabel отримує картинку, коли вона прийде."""

    def __init__(self, cache_dir):
        self.nam = QNetworkAccessManager()
        cache = QNetworkDiskCache()
        cache.setCacheDirectory(cache_dir)
        cache.setMaximumCacheSize(300 * 1024 * 1024)
        self.nam.setCache(cache)

    def load(self, url, target, size):
        if not url:
            return
        req = QNetworkRequest(QUrl(url))
        req.setAttribute(QNetworkRequest.CacheLoadControlAttribute,
                         QNetworkRequest.PreferCache)
        req.setHeader(QNetworkRequest.UserAgentHeader, core.UA)
        reply = self.nam.get(req)

        def finished():
            data = reply.readAll()
            reply.deleteLater()
            pm = QPixmap()
            if pm.loadFromData(bytes(data)):
                try:
                    target.setProperty("loaded", True)
                    target.setStyleSheet("background:#ffffff;")
                    target.setPixmap(pm.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                except RuntimeError:
                    pass  # картку вже прибрали
        reply.finished.connect(finished)


class ProductCard(QFrame):
    def __init__(self, win, p, width):
        super().__init__()
        CARD_W = width
        self.setObjectName("product")
        self.setFixedWidth(CARD_W)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Maximum)
        t = win.tr_
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(6)

        photo_box = QWidget()
        pl = QGridLayout(photo_box)
        pl.setContentsMargins(0, 0, 0, 0)
        photo = label("🛒", "photo")
        photo.setAlignment(Qt.AlignCenter)
        photo.setFixedSize(CARD_W - 24, 160)
        pl.addWidget(photo, 0, 0)
        if p["old_price"] and p["discount"]:
            badge = label("-%d%%%s" % (p["discount"], "  " + t("until", d=p["promo_until"])
                                       if p["promo_until"] else ""), "badge")
            pl.addWidget(badge, 0, 0, Qt.AlignTop | Qt.AlignLeft)
        lay.addWidget(photo_box)
        win.images.load(p["img"], photo, QSize(CARD_W - 40, 150))

        name = label(p["name"] or p["slug"], "pname", wrap=True)
        name.setMinimumHeight(38)
        lay.addWidget(name)

        prow = QHBoxLayout()
        prow.setSpacing(8)
        unit = t("per_kg") if p["unit"] == "кг" else ""
        prow.addWidget(label("%.2f ₴%s" % (p["price"], unit), "price"))
        if p["old_price"]:
            prow.addWidget(label("%.2f" % p["old_price"], "oldprice"))
        prow.addStretch(1)
        lay.addLayout(prow)

        tier = core.tier(p)
        color = theme.TIER_COLORS[tier]
        key = "nutri" if p["sugar"] is not None else "nutri_nosugar"
        nut = t(key, kcal=round(p["kcal"]), sugar=fnum(p["sugar"], win.lang),
                carbs=fnum(p.get("carbs"), win.lang), protein=fnum(p["protein"], win.lang))
        lay.addWidget(label('<span style="color:%s">●</span> %s' % (color, html.escape(nut)),
                            wrap=True))
        src = t("src_atb") if p["src"] == "atb" else t("src_off")
        if p["nutriscore"]:
            src += " · Nutri-Score %s" % p["nutriscore"]
        lay.addWidget(label(src, "small"))

        k_used, s_used = win.store.totals()
        por = core.portion(p, win.daily_kcal - k_used, win.sugar_g - s_used, win.snack_kcal)
        unit_g = "мл" if p["liquid"] else ("g" if win.lang == "en" else "г")
        if p["liquid"] and win.lang == "en":
            unit_g = "ml"
        txt, pcolor = self.portion_text(win, p, por, unit_g)
        pl_ = label(txt, wrap=True)
        pl_.setStyleSheet("color:%s; font-weight:600;" % pcolor)
        lay.addWidget(pl_)
        lay.addStretch(1)

        brow = QHBoxLayout()
        brow.setSpacing(6)
        grams = por.get("grams")
        if grams:
            w = p["weight"]
            if w and grams >= w:
                b = QPushButton(t("drank" if p["liquid"] else "ate"))
                b.setObjectName("eat")
                b.clicked.connect(lambda: win.eat(p, w))
                brow.addWidget(b)
                h = QPushButton(t("half"))
                h.clicked.connect(lambda: win.eat(p, w / 2))
                brow.addWidget(h)
            else:
                b = QPushButton(t("drank_g" if p["liquid"] else "ate_g",
                                  g="%d %s" % (grams, unit_g)))
                b.setObjectName("eat")
                b.clicked.connect(lambda: win.eat(p, grams))
                brow.addWidget(b)
        brow.addStretch(1)
        o = QPushButton(t("open_atb"))
        o.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl("%s/product/%s" % (core.SITE, p["slug"]))))
        brow.addWidget(o)
        lay.addLayout(brow)

    @staticmethod
    def portion_text(win, p, por, u):
        t = win.tr_
        k = por["kind"]
        if k == "unknown":
            return t("p_unknown"), theme.MUTED
        if k in ("day_full", "sugar_full"):
            return t("p_" + k), theme.ERR
        if k == "free":
            return "✓ " + t("p_free"), theme.OK
        extra = t("p_by_sugar") if por.get("by_sugar") else ""
        w = "%s %s" % (fnum(p["weight"], win.lang), u) if p["weight"] else ""
        if k == "packs":
            return "✓ " + t("p_packs", n=por["packs"], w=w, kcal=round(por["kcal"])) + extra, theme.OK
        if k == "whole":
            return "✓ " + t("p_whole", w=w, kcal=round(por["kcal"])) + extra, theme.OK
        g = "%d %s" % (por["grams"], u)
        if por.get("frac"):
            f = t("of_pack", f=core.frac_symbol(por["frac"]))
            return "🍽 " + t("p_part", g=g, frac=f, kcal=round(por["kcal"])) + extra, theme.TEXT
        return "🍽 " + t("p_part_nofrac", g=g, kcal=round(por["kcal"])) + extra, theme.TEXT


class SettingsDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        t = win.tr_
        self.setWindowTitle(t("set_title"))
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        form = QFormLayout()
        form.setSpacing(10)

        def spin(lo, hi, val, step):
            s = QSpinBox()
            s.setRange(lo, hi)
            s.setSingleStep(step)
            s.setValue(int(val))
            return s
        self.daily = spin(800, 5000, win.daily_kcal, 50)
        self.snack = spin(50, 1500, win.snack_kcal, 25)
        self.sugar = spin(0, 200, win.sugar_g, 5)
        self.lang = QComboBox()
        for code, name in LANGS.items():
            self.lang.addItem(name, code)
        self.lang.setCurrentIndex(list(LANGS).index(win.lang))
        form.addRow(t("set_daily"), self.daily)
        form.addRow(t("set_snack"), self.snack)
        form.addRow(t("set_sugar"), self.sugar)
        form.addRow(t("language"), self.lang)
        lay.addLayout(form)
        lay.addSpacing(6)
        lay.addWidget(label(t("set_help"), "muted", wrap=True))
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText(t("save"))
        bb.button(QDialogButtonBox.Cancel).setText(t("cancel"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class MainWindow(QWidget):
    def __init__(self, db_path=None):
        super().__init__()
        self.settings = QSettings("lasun", "lasun")
        sys_lang = (QApplication.instance().property("lang_hint") or "en")
        self.lang = self.settings.value("lang", sys_lang if sys_lang in LANGS else "en")
        self.daily_kcal = int(self.settings.value("daily_kcal", core.DEFAULTS["daily_kcal"]))
        self.snack_kcal = int(self.settings.value("snack_kcal", core.DEFAULTS["snack_kcal"]))
        self.sugar_g = int(self.settings.value("sugar_g", core.DEFAULTS["sugar_g"]))
        self.db_path = db_path or os.path.join(data_dir(), "lasun.db")
        self.store = core.Store(self.db_path)
        self.images = Images(os.path.join(data_dir(), "images"))
        self.view = self.settings.value("view", "promo")
        if self.view not in NAV:
            self.view = "promo"
        self.limit = PAGE
        self.crawler = None
        self.status_override = None

        self.setWindowTitle("Lasun" if self.lang == "en" else "Ласун")
        self.setWindowIcon(QIcon(desktop.ICON))
        self.resize(1240, 820)
        self.setMinimumSize(900, 600)
        self.build()
        self.retranslate()
        self.refresh_view()
        self.refresh_today()

        self.search_timer = QTimer(self, singleShot=True, interval=250)
        self.search_timer.timeout.connect(self.reset_and_refresh)
        self.day = core.today()
        self.day_timer = QTimer(self, interval=60_000)
        self.day_timer.timeout.connect(self.check_day)
        self.day_timer.start()
        if time.time() - self.store.last_crawl() > STALE_SEC:
            QTimer.singleShot(400, self.start_crawl)

    # ---------- побудова ----------
    def tr_(self, key, **kw):
        return STRINGS[self.lang].get(key, STRINGS["en"].get(key, key)).format_map(_Args(kw))

    def build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(14)

        top = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(QIcon(desktop.ICON).pixmap(40, 40))
        top.addWidget(icon)
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        self.title = label("", "title")
        self.subtitle = label("", "muted")
        tcol.addWidget(self.title)
        tcol.addWidget(self.subtitle)
        top.addLayout(tcol)
        top.addStretch(1)
        scol = QVBoxLayout()
        scol.setSpacing(4)
        self.status = label("", "muted")
        self.status.setAlignment(Qt.AlignRight)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedWidth(280)
        self.bar.hide()
        scol.addWidget(self.status)
        scol.addWidget(self.bar, 0, Qt.AlignRight)
        top.addLayout(scol)
        self.refresh_btn = QPushButton()
        self.refresh_btn.clicked.connect(self.toggle_crawl)
        self.settings_btn = QPushButton()
        self.settings_btn.clicked.connect(self.edit_settings)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.settings_btn)
        root.addLayout(top)

        body = QHBoxLayout()
        body.setSpacing(18)

        nav = QVBoxLayout()
        nav.setSpacing(4)
        self.nav_group = QButtonGroup(self)
        self.nav_btns = {}
        for key in NAV:
            b = QPushButton()
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setChecked(key == self.view)
            b.clicked.connect(lambda _=False, k=key: self.set_view(k))
            self.nav_group.addButton(b)
            self.nav_btns[key] = b
            nav.addWidget(b)
        nav.addSpacing(14)
        self.only_good = QCheckBox()
        self.only_good.setChecked(self.settings.value("only_good", "false") == "true")
        self.only_good.toggled.connect(self.reset_and_refresh)
        nav.addWidget(self.only_good)
        nav.addSpacing(6)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda: self.search_timer.start())
        nav.addWidget(self.search)
        nav.addStretch(1)
        navw = QWidget()
        navw.setLayout(nav)
        navw.setFixedWidth(220)
        body.addWidget(navw)

        center = QVBoxLayout()
        center.setSpacing(8)
        self.header = label("", "bigaction")
        self.legend = label("", "small")
        center.addWidget(self.header)
        center.addWidget(self.legend)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 4, 8, 4)
        self.grid.setHorizontalSpacing(GAP)
        self.grid.setVerticalSpacing(GAP)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(self.grid_host)
        center.addWidget(self.scroll, 1)
        body.addLayout(center, 1)

        today = QFrame()
        today.setObjectName("card")
        today.setFixedWidth(290)
        tl = QVBoxLayout(today)
        tl.setContentsMargins(18, 16, 18, 16)
        tl.setSpacing(8)
        self.today_title = label("", "section")
        tl.addWidget(self.today_title)
        self.kcal_lbl = label("", "bigaction")
        tl.addWidget(self.kcal_lbl)
        self.kcal_bar = QProgressBar()
        self.kcal_bar.setObjectName("kcalbar")
        self.kcal_bar.setTextVisible(False)
        tl.addWidget(self.kcal_bar)
        self.sugar_lbl = label("", "muted")
        tl.addWidget(self.sugar_lbl)
        self.sugar_bar = QProgressBar()
        self.sugar_bar.setTextVisible(False)
        tl.addWidget(self.sugar_bar)
        tl.addSpacing(6)
        self.eaten_box = QVBoxLayout()
        self.eaten_box.setSpacing(4)
        eaten_w = QWidget()
        eaten_w.setLayout(self.eaten_box)
        es = QScrollArea()
        es.setWidgetResizable(True)
        es.setWidget(eaten_w)
        tl.addWidget(es, 1)
        add = QHBoxLayout()
        self.other_kcal = QSpinBox()
        self.other_kcal.setRange(0, 3000)
        self.other_kcal.setSingleStep(50)
        self.other_kcal.setValue(200)
        self.add_btn = QPushButton()
        self.add_btn.clicked.connect(self.add_other)
        add.addWidget(self.other_kcal, 1)
        add.addWidget(self.add_btn)
        tl.addLayout(add)
        body.addWidget(today)
        root.addLayout(body, 1)

    def retranslate(self):
        t = self.tr_
        self.title.setText("Lasun" if self.lang == "en" else "Ласун")
        self.setWindowTitle(self.title.text())
        self.subtitle.setText(t("subtitle"))
        for k, b in self.nav_btns.items():
            b.setText("%s  %s" % (NAV_ICON[k], t("nav_" + k)))
        self.only_good.setText(t("only_good"))
        self.search.setPlaceholderText(t("search"))
        self.legend.setText(t("legend"))
        self.settings_btn.setText(t("settings"))
        self.today_title.setText(t("today"))
        self.other_kcal.setSuffix(" " + t("kcal"))
        self.add_btn.setText(t("add_other"))
        self.update_status()

    # ---------- картки ----------
    def set_view(self, key):
        self.view = key
        self.nav_btns[key].setChecked(True)
        self.settings.setValue("view", key)
        self.limit = PAGE
        self.refresh_view()
        self.scroll.verticalScrollBar().setValue(0)

    def reset_and_refresh(self):
        self.settings.setValue("only_good", "true" if self.only_good.isChecked() else "false")
        self.limit = PAGE
        self.refresh_view()

    def columns(self):
        avail = self.scroll.viewport().width() - 12
        cols = max(1, (avail + GAP) // (CARD_MIN_W + GAP))
        return cols, max(CARD_MIN_W, (avail - GAP * (cols - 1)) // cols)

    def refresh_view(self):
        t = self.tr_
        q = self.search.text().strip()
        rows, unknown = self.store.products(self.view, self.only_good.isChecked(), q)
        title = t("nav_" + self.view)
        self.header.setText(t("header_unknown" if unknown else "header", title=title,
                              n=len(rows), u=unknown))
        clear_layout(self.grid)
        if not rows:
            if q:
                msg = t("empty_search", q=q)
            elif not self.store.counts()[0]:
                msg = t("empty_first")
            elif unknown:
                msg = t("empty_unknown", u=unknown)
            else:
                msg = t("empty_none")
            m = label(msg, "muted", wrap=True)
            m.setMinimumWidth(420)
            self.grid.addWidget(m, 0, 0)
            return
        cols, width = self.columns()
        self._cols = (cols, width)
        for i, p in enumerate(rows[:self.limit]):
            self.grid.addWidget(ProductCard(self, p, width), i // cols, i % cols, Qt.AlignTop)
        left = len(rows) - self.limit
        if left > 0:
            more = QPushButton(t("show_more", n=left))
            more.clicked.connect(self.show_more)
            self.grid.addWidget(more, (self.limit + cols - 1) // cols, 0, 1, cols)

    def show_more(self):
        pos = self.scroll.verticalScrollBar().value()
        self.limit += PAGE
        self.refresh_view()
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(pos))

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.refresh_view_keep_scroll)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if getattr(self, "_cols", None) and self.columns() != self._cols:
            QTimer.singleShot(0, self.refresh_view)

    # ---------- сьогодні ----------
    def eat(self, p, grams):
        self.store.eat_product(p, grams)
        self.refresh_today()
        self.refresh_view_keep_scroll()

    def add_other(self):
        k = self.other_kcal.value()
        if k > 0:
            self.store.eat(None, self.tr_("other"), 0, k, 0)
            self.refresh_today()
            self.refresh_view_keep_scroll()

    def undo(self, eid):
        self.store.undo(eid)
        self.refresh_today()
        self.refresh_view_keep_scroll()

    def refresh_view_keep_scroll(self):
        pos = self.scroll.verticalScrollBar().value()
        self.refresh_view()
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(pos))

    def refresh_today(self):
        t = self.tr_
        k, s = self.store.totals()
        self.kcal_lbl.setText(t("kcal_of", used=round(k), total=self.daily_kcal))
        self.kcal_bar.setRange(0, max(1, self.daily_kcal))
        self.kcal_bar.setValue(min(int(k), self.daily_kcal))
        self.sugar_lbl.setText(t("sugar_of", used=fnum(round(s, 1), self.lang), total=self.sugar_g))
        self.sugar_lbl.setStyleSheet("color:%s" % (theme.ERR if s > self.sugar_g else theme.MUTED))
        self.sugar_bar.setRange(0, max(1, self.sugar_g))
        self.sugar_bar.setValue(min(int(s), self.sugar_g))
        over = k > self.daily_kcal
        self.kcal_lbl.setStyleSheet("color:%s" % (theme.ERR if over else theme.TEXT))
        clear_layout(self.eaten_box)
        rows = self.store.eaten()
        if not rows:
            self.eaten_box.addWidget(label(t("today_empty"), "muted", wrap=True))
        for r in reversed(rows):
            line = QHBoxLayout()
            amount = ("%d %s · " % (r["grams"], "g" if self.lang == "en" else "г")) if r["grams"] else ""
            lbl = label("%s<br><span style='color:%s'>%s%d %s</span>" % (
                html.escape(r["name"] or "?"), theme.MUTED, amount, r["kcal"], t("kcal")), wrap=True)
            x = QPushButton("✕")
            x.setObjectName("ghost")
            x.clicked.connect(lambda _=False, eid=r["id"]: self.undo(eid))
            w = QWidget()
            line.setContentsMargins(0, 0, 0, 0)
            line.addWidget(lbl, 1)
            line.addWidget(x, 0, Qt.AlignTop)
            w.setLayout(line)
            self.eaten_box.addWidget(w)
        self.eaten_box.addStretch(1)

    def check_day(self):
        if core.today() != self.day:
            self.day = core.today()
            self.refresh_today()
            self.refresh_view_keep_scroll()
        if not self.crawler and time.time() - self.store.last_crawl() > STALE_SEC + 6 * 3600:
            self.start_crawl()

    # ---------- оновлення ----------
    def toggle_crawl(self):
        if self.crawler:
            self.crawler.cancel.set()
            self.refresh_btn.setEnabled(False)
        else:
            self.start_crawl()

    def start_crawl(self):
        if self.crawler:
            return
        self.status_override = None
        self.crawler = Crawler(self.db_path)
        self.crawler.progress.connect(self.on_progress)
        self.crawler.done.connect(self.on_done)
        self.crawler.failed.connect(self.on_failed)
        self.crawler.finished.connect(self.on_finished)
        self.bar.setRange(0, 0)
        self.bar.show()
        self.crawler.start()
        self.update_status()

    def on_progress(self, stage, done, total):
        if stage == "done":
            return
        self.status_override = self.tr_("st_" + stage, done=done, total=total)
        self.bar.setRange(0, max(total, 1))
        self.bar.setValue(done)
        self.update_status()
        # ціни вже є після першого етапу — показати їх; калорії — кожні ~200 товарів
        if (stage == "list" and done == total) or (stage == "detail" and done % 200 == 0):
            self.refresh_view_keep_scroll()

    def on_done(self, n):
        self.status_override = None
        self.refresh_view_keep_scroll()

    def on_failed(self, code, detail):
        if code == "cancelled":
            self.status_override = self.tr_("st_cancelled")
        else:
            self.status_override = "⚠ " + self.tr_(code, detail=detail)
        self.refresh_view_keep_scroll()

    def on_finished(self):
        self.crawler = None
        self.bar.hide()
        self.refresh_btn.setEnabled(True)
        self.update_status()

    def update_status(self):
        t = self.tr_
        self.refresh_btn.setText(t("stop") if self.crawler else t("refresh"))
        if self.status_override:
            self.status.setText(self.status_override)
            self.status.setStyleSheet("color:%s" % (theme.ERR if self.status_override.startswith("⚠")
                                                    else theme.MUTED))
            return
        self.status.setStyleSheet("")
        last = float(self.store.get("last_crawl_ok", 0) or 0)
        if last:
            when = dt.datetime.fromtimestamp(last).strftime("%d.%m %H:%M")
            self.status.setText(t("st_updated", when=when))
        else:
            self.status.setText(t("st_never"))

    # ---------- налаштування ----------
    def edit_settings(self):
        d = SettingsDialog(self)
        if d.exec() != QDialog.Accepted:
            return
        self.daily_kcal, self.snack_kcal, self.sugar_g = (d.daily.value(), d.snack.value(),
                                                          d.sugar.value())
        self.lang = d.lang.currentData()
        for k in ("daily_kcal", "snack_kcal", "sugar_g", "lang"):
            self.settings.setValue(k, getattr(self, k))
        self.retranslate()
        self.refresh_today()
        self.refresh_view_keep_scroll()

    def closeEvent(self, e):
        if self.crawler:
            self.crawler.cancel.set()
            self.crawler.wait(8000)
        super().closeEvent(e)


def selftest(out_path):
    """Для CI: вікно з тестовими товарами, всі розділи, налаштування, знімок екрана."""
    import tempfile
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "t.db")
    st = core.Store(db)
    now = time.time()
    demo = [
        ("jog", "Йогурт 280г Дольче Персик-маракуйя 3,2%", "dairy", 31.9, 44.4, 28, "20.10", 280, 0, 95, 12.1, 3, "atb"),
        ("cola", "Напій 0,5л Coca-Cola Zero", "drinks", 29.9, None, None, None, 500, 1, 0.3, 0, 0, "atb"),
        ("ice", "Морозиво 70г Пломбір у вафельному стаканчику", "icecream", 25.0, 30.0, 17, "21.10", 70, 0, 230, 21, 3.5, "off"),
        ("chips", "Чипси 60г зі смаком сметани", "snacks", 40, None, None, None, 60, 0, 520, 1, 6, "atb"),
        ("bar", "Батончик 35г протеїновий без цукру", "sweets", 45.5, 55.0, 17, "22.10", 35, 0, 340, 0.5, 30, "atb"),
    ]
    for r in demo:
        st.con.execute("INSERT INTO products (slug,name,grp,price,old_price,discount,promo_until,"
                       "weight,liquid,kcal,sugar,protein,src,seen_ts,unit,detail_ts) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'шт',?)", r + (now, now))
    st.con.commit()
    st.set("last_crawl", now)
    st.set("last_crawl_ok", now)
    win = MainWindow(db_path=db)
    win.show()
    QApplication.processEvents()
    lines = []
    for v in NAV:
        win.set_view(v)
        QApplication.processEvents()
        rows, unk = win.store.products(v)
        lines.append("%s: %d" % (v, len(rows)))
    win.set_view("promo")
    p = win.store.products("dairy")[0][0]
    win.eat(p, 280)
    QApplication.processEvents()
    lines.append("today: %s" % (win.store.totals(),))
    win.grab().save(out_path.rsplit(".", 1)[0] + ".png")
    SettingsDialog(win).close()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\nOK\n")
    win.close()
    return 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) > 2 and sys.argv[1] == "--check-site":
        lines, page = core.check_site()
        with open(sys.argv[2], "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        with open(sys.argv[2] + ".html", "w", encoding="utf-8") as f:
            f.write(page)
        print("\n".join(lines))
        return 0
    QApplication.setApplicationName("lasun")   # дані: ~/.local/share/lasun, %APPDATA%\\lasun
    app = QApplication(sys.argv)
    from PySide6.QtCore import QLocale
    app.setProperty("lang_hint", QLocale.system().name()[:2])
    app.setWindowIcon(QIcon(desktop.ICON))
    theme.apply(app)
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        return selftest(sys.argv[2])
    desktop.install_menu_entry()
    win = MainWindow()
    win.show()
    return app.exec()
