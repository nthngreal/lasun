"""Розбір сторінок АТБ і порції: реалістичні шматки тексту, які ми бачили на сайті."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lasun import core  # noqa: E402

CARD = ('<div class="catalog-item"><div class="tag">-28% до 20.10</div>'
        '<a href="/product/{slug}"><img src="https://src.zakaz.atbmarket.com/cache/photos/'
        '37229/catalog_list_37229.jpg" alt="Йогурт 280г Дольче Персик-маракуйя 3,2%"></a>'
        '<div class="title"><a href="/product/{slug}">Йогурт 280г Дольче</a></div>'
        '<div class="price"><span>31.90</span> грн/шт <span class="old">44.40</span></div></div>')
PLAIN = ('<div class="catalog-item"><a href="/product/{slug}"><img alt="Сік 1,93л Садочок"></a>'
         '<div class="price">89.90 грн/шт</div></div>')


class NutritionTest(unittest.TestCase):
    def test_atb_kabanosy_text(self):
        t = ("Склад: свинина, цукор, сіль. Енергетична цінність /калорійність 2192,8 кДж/524,1ккал. "
             "Жири загалом 43,3г,насичені 15,1г. Вуглеводи 9.4г, цукри 0.9г, білки 24.2 г")
        d = core.parse_nutrition(t)
        self.assertEqual((d["kcal"], d["sugar"], d["protein"], d["fat"]), (524.1, 0.9, 24.2, 43.3))

    def test_ingredient_sugar_is_not_nutrition(self):
        d = core.parse_nutrition("Склад: молоко, цукор 12%, закваска. Харчова цінність в 100 г: "
                                 "білки – 3,0 г, в т.ч. цукри – 9,5 г. Енергетична цінність – 95 ккал")
        self.assertEqual((d["kcal"], d["sugar"]), (95.0, 9.5))

    def test_kj_only(self):
        self.assertAlmostEqual(core.parse_nutrition("Енергетична цінність 418 кДж")["kcal"], 99.9, 1)

    def test_atb_characteristics_block(self):
        # так виглядає блок «Харчові властивості» на сторінці товару АТБ (14.10.2026)
        t = ("Склад: молоко, закваска бактеріальна.\n Харчові властивості \n Калорійність \n "
             "1791 кДж/100г \n Вуглеводи \n 88.1 г/100г \n Білки \n 2 г/100г \n Жири \n "
             "2 г/100г \n Загальна інформація \n Торгова марка \n Дольче")
        d = core.parse_nutrition(t)
        self.assertAlmostEqual(d["kcal"], 428.1, 1)
        self.assertEqual((d.get("sugar"), d["carbs"], d["protein"], d["fat"]), (None, 88.1, 2.0, 2.0))

    def test_no_nutrition(self):
        self.assertEqual(core.parse_nutrition("Склад: какао терте, цукор 45%."), {})

    def test_weight(self):
        self.assertEqual(core.parse_weight("Сік 1,93л Садочок"), (1930.0, True))
        self.assertEqual(core.parse_weight("Сирок 4х40г"), (160.0, False))
        self.assertEqual(core.parse_weight("Шоколад Рошен"), (None, False))


class CardsTest(unittest.TestCase):
    def test_cards_do_not_steal_neighbour_badge(self):
        html = CARD.format(slug="a") + PLAIN.format(slug="b") + CARD.format(slug="c")
        cards = dict(core.split_cards(html))
        self.assertEqual(list(cards), ["a", "b", "c"])
        a, b = core.parse_card("a", cards["a"]), core.parse_card("b", cards["b"])
        self.assertEqual((a["price"], a["old_price"], a["discount"], a["promo_until"]),
                         (31.9, 44.4, 28, "20.10"))
        self.assertIn("37229", a["img"])
        self.assertEqual((b["price"], b["old_price"], b["discount"]), (89.9, None, None))
        self.assertEqual(b["name"], "Сік 1,93л Садочок")


class PortionTest(unittest.TestCase):
    def p(self, **kw):
        base = {"kcal": 95, "sugar": 4, "weight": 140, "liquid": 0}
        base.update(kw)
        return base

    def test_whole_and_packs(self):
        self.assertEqual(core.portion(self.p(), 1800, 30, 250)["kind"], "whole")
        self.assertEqual(core.portion(self.p(kcal=48), 1800, 30, 250)["packs"], 3)

    def test_sugar_limits(self):
        r = core.portion(self.p(sugar=12, weight=280), 1800, 30, 250)
        self.assertTrue(r["by_sugar"])
        self.assertEqual(r["grams"], 250)

    def test_day_full(self):
        self.assertEqual(core.portion(self.p(), 20, 30, 250)["kind"], "day_full")

    def test_zero_drink(self):
        self.assertEqual(core.portion(self.p(kcal=0.3, sugar=0, liquid=1), 0, 0, 250)["kind"], "free")


class TierTest(unittest.TestCase):
    def test_unknown_sugar_never_green(self):
        p = {"kcal": 60, "sugar": None, "carbs": 12, "liquid": 0}
        self.assertEqual(core.tier(p), core.YELLOW)
        p["carbs"] = 3          # вуглеводів мало — отже, і цукру мало
        self.assertEqual(core.tier(p), core.GREEN)


class StoreTest(unittest.TestCase):
    def test_promo_first_and_hidden_heavy(self):
        st = core.Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        now = time.time()
        for slug, old, kcal, sugar in (("light", None, 50, 2), ("deal", 40.0, 90, 8),
                                       ("heavy_deal", 40.0, 500, 30), ("nodata", None, None, None)):
            st.con.execute("INSERT INTO products (slug,name,grp,price,old_price,discount,kcal,sugar,"
                           "liquid,seen_ts) VALUES (?,?,?,?,?,?,?,?,0,?)",
                           (slug, slug, "dairy", 30.0, old, 25 if old else None, kcal, sugar, now))
        st.set("last_crawl", now)
        rows, unknown = st.products("dairy")
        self.assertEqual([r["slug"] for r in rows], ["deal", "light", "heavy_deal"])
        self.assertEqual(unknown, 1)
        self.assertEqual([r["slug"] for r in st.products("promo")[0]], ["deal"])


if __name__ == "__main__":
    unittest.main()


class CrawlTest(unittest.TestCase):
    """Повний обхід на підставних сторінках: списки → склад → база."""

    def test_crawl_fills_prices_and_calories(self):
        pages = {
            "/catalog/349-yogurti": CARD.format(slug="jog") + PLAIN.format(slug="sik"),
            "/product/jog": "<h1>Йогурт 280г Дольче</h1><p>Харчова цінність в 100 г: "
                            "білки 3 г, цукри 9,5 г. Енергетична цінність 95 ккал</p>",
            "/product/sik": "<h1>Сік 1,93л Садочок</h1><p>Склад: сік.</p>",
        }

        def fake_get(url, **kw):
            if "openfoodfacts" in url:
                return '{"products": []}'
            return pages.get(url.replace(core.SITE, ""))
        old_get, old_groups, old_count = core.http_get, core.GROUPS, core.CATEGORY_COUNT
        core.http_get = fake_get
        core.GROUPS = [("dairy", False, ["349-yogurti"])]
        core.CATEGORY_COUNT = 1
        orig_wait = core.threading.Event.wait
        core.threading.Event.wait = lambda self, t=None: self.is_set()  # без пауз у тесті
        try:
            path = os.path.join(tempfile.mkdtemp(), "t.db")
            self.assertEqual(core.crawl(path), 2)
        finally:
            core.http_get, core.GROUPS, core.CATEGORY_COUNT = old_get, old_groups, old_count
            core.threading.Event.wait = orig_wait
        st = core.Store(path)
        rows, unknown = st.products("dairy")
        self.assertEqual([(r["slug"], r["kcal"], r["sugar"], r["weight"], r["old_price"])
                          for r in rows], [("jog", 95.0, 9.5, 280.0, 44.4)])
        self.assertEqual(unknown, 1)
