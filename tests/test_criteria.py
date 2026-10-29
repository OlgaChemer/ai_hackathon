"""Проверка критериев подбора: 5 обязательных + дополнительные, порог 70%."""
import unittest

from backend.agent import criteria as C
from backend.agent import tools as T


def profile(**kw):
    p = dict(T.EMPTY_PROFILE)
    p.update(districts=[], wishes=[], infra=[], material=[])
    p.update(kw)
    return p


def listing(**kw):
    base = {
        "id": "x1", "city": "Казань", "rooms": 2, "price": 8_000_000,
        "condition": "косметика", "floor": 5, "floors": 10, "area": 50.0,
        "kitchen": 9.0, "year": 2005, "material": "кирпич",
        "metro": {"name": "Козья слобода", "minutes": 12, "mode": "пешком"},
    }
    base.update(kw)
    return base


class CriteriaSetTest(unittest.TestCase):
    def test_core_is_five(self):
        self.assertEqual(len(C.CORE), 5)
        self.assertEqual(set(C.CORE), {"city", "budget_max", "rooms", "condition", "metro_max_minutes"})

    def test_missing_core(self):
        p = profile(city="Казань", rooms=2)
        self.assertEqual(set(C.missing_core(p)), {"budget_max", "condition", "metro_max_minutes"})

    def test_any_counts_as_answer(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000, condition="any", metro_any=True)
        self.assertEqual(C.missing_core(p), [])

    def test_extra_count(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000, condition="any", metro_any=True)
        self.assertEqual(C.extra_count(p), 0)
        p.update(floor_any=True, area_any=True, house_any=True)
        self.assertEqual(C.extra_count(p), 3)


class MatchTest(unittest.TestCase):
    def test_full_match(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="cosmetic", metro_max_minutes=15)
        res = C.check(p, listing())
        self.assertEqual((res["ok"], res["checked"], res["pct"]), (5, 5, 1.0))
        self.assertTrue(C.matches(p, listing()))

    def test_one_fail_still_matches(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="cosmetic", metro_max_minutes=15, not_first_floor=True)
        res = C.check(p, listing(floor=1))
        self.assertEqual(res["checked"], 6)
        self.assertEqual(res["pct"], 0.833)
        self.assertIn("floor", res["failed"])
        self.assertTrue(C.matches(p, listing(floor=1)))

    def test_three_fails_rejected(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="ready", metro_max_minutes=10, area_min=60)
        res = C.check(p, listing(price=999_000_000, condition="требует ремонта"))
        self.assertFalse(C.matches(p, listing(price=999_000_000, condition="требует ремонта")))
        self.assertLess(res["pct"], 0.7)

    def test_unknown_data_does_not_count(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="cosmetic", metro_max_minutes=15)
        res = C.check(p, listing(condition=None, metro=None))
        self.assertEqual(res["checked"], 3)          # город, бюджет, комнаты
        self.assertEqual(res["unknown"], ["condition", "metro_max_minutes"])
        self.assertEqual(res["pct"], 1.0)

    def test_condition_ranking(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="ready", metro_any=True)
        ok = C.check(p, listing(condition="евроремонт"))
        bad = C.check(p, listing(condition="требует ремонта"))
        self.assertNotIn("condition", ok["failed"])
        self.assertIn("condition", bad["failed"])

    def test_budget_tolerance(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="any", metro_any=True)
        self.assertTrue(C.matches(p, listing(price=9_400_000)))   # +4,4% — можно сторговать
        self.assertFalse(C.matches(p, listing(price=9_600_000)))  # +6,7% — уже нет

    def test_wrong_rooms_and_city(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="any", metro_any=True)
        self.assertFalse(C.matches(p, listing(rooms=3)))
        self.assertFalse(C.matches(p, listing(city="Москва")))


class SelectTest(unittest.TestCase):
    def test_threshold_keeps_only_matching(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="ready", metro_max_minutes=10)
        good = listing(id="good", condition="евроремонт", metro={"minutes": 5, "mode": "пешком"})
        bad = listing(id="bad", condition="требует ремонта", metro={"minutes": 40, "mode": "транспортом"})
        sel, info = C.select(p, [good, bad], limit=1)
        self.assertEqual([x["id"] for x in sel], ["good"])
        self.assertEqual(info["threshold"], 0.7)

    def test_relax_when_few(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="ready", metro_max_minutes=10, area_min=60)
        bad = listing(id="bad", condition="требует ремонта", metro={"minutes": 40, "mode": "транспортом"})
        sel, info = C.select(p, [bad], limit=10)
        self.assertLess(info["threshold"], 0.7)
        self.assertTrue(info["relaxed"])

    def test_rank_prefers_better_match(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="ready", metro_max_minutes=10)
        a = listing(id="a", condition="евроремонт", metro={"minutes": 5, "mode": "пешком"})
        b = listing(id="b", condition="косметика", metro={"minutes": 20, "mode": "пешком"})
        self.assertEqual([x["id"] for x in C.rank(p, [b, a])], ["a", "b"])


class ParseTest(unittest.TestCase):
    def test_rules_parse_new_criteria(self):
        p = T.parse_request_rules(
            "Двушка в Казани до 9 млн, косметический ремонт, не первый и не последний этаж, "
            "метро до 10 минут, кирпич, не старше 1990, рядом школа, срочно"
        )
        self.assertEqual(p["condition"], "cosmetic")
        self.assertTrue(p["not_first_floor"])
        self.assertTrue(p["not_last_floor"])
        self.assertEqual(p["metro_max_minutes"], 10)
        self.assertIn("кирпич", p["material"])
        self.assertEqual(p["year_min"], 1990)
        self.assertEqual(p["infra"], ["школа"])
        self.assertEqual(p["timeline"], "month")

    def test_summary_mentions_criteria(self):
        p = T.merge_profile({"city": "Казань", "rooms": 2, "budget_max": 9_000_000,
                             "condition": "cosmetic", "metro_max_minutes": 10})
        text = T.profile_summary(p)
        self.assertIn("Казань", text)
        self.assertIn("косметический", text)
        self.assertIn("метро", text)


if __name__ == "__main__":
    unittest.main()
