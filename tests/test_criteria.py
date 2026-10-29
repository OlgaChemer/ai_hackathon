"""Критерии подбора: 5 обязательных + дополнительные, три исхода и порог 70%."""
import unittest

from backend.agent import criteria as C
from backend.agent import tools as T


def profile(**kw):
    p = dict(T.EMPTY_PROFILE)
    p.update(districts=[], wishes=[], infra=[], material=[], features=[])
    p.update(kw)
    return p


def listing(**kw):
    base = {
        "id": "x1", "city": "Казань", "rooms": 2, "price": 8_000_000,
        "condition": "косметика", "floor": 5, "floors": 10, "area": 50.0,
        "kitchen": 9.0, "year": 2005, "material": "кирпич",
        "metro": {"name": "Козья слобода", "minutes": 12, "mode": "пешком"},
        "description": "Светлая квартира, окна во двор",
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
        self.assertEqual(C.status(p, listing()), "fits")

    def test_explicit_mismatch_rejects(self):
        """Один явный промах — квартиру не показываем, даже если остальное сошлось."""
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="cosmetic", metro_max_minutes=15, not_first_floor=True)
        bad = listing(floor=1)
        res = C.check(p, bad)
        self.assertEqual(res["failed"], ["floor"])
        self.assertEqual(C.status(p, bad), "rejected")
        self.assertFalse(C.matches(p, bad))

    def test_unknown_counts_in_denominator(self):
        """«7 из 10»: критерии без данных в объявлении считаются неподтверждёнными."""
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="cosmetic", metro_max_minutes=15)
        res = C.check(p, listing(condition=None, metro=None))
        self.assertEqual(res["checked"], 5)          # все заданные критерии
        self.assertEqual(res["ok"], 3)               # город, бюджет, комнаты
        self.assertEqual(res["pct"], 0.6)
        self.assertEqual(res["unknown"], ["condition", "metro_max_minutes"])
        self.assertEqual(res["failed"], [])

    def test_too_many_unknown_is_unverified(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="cosmetic", metro_max_minutes=15, not_first_floor=True)
        x = listing(condition=None, metro=None, floor=None, floors=None)
        self.assertEqual(C.status(p, x), "unverified")

    def test_condition_ranking(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="ready", metro_any=True)
        self.assertNotIn("condition", C.check(p, listing(condition="евроремонт"))["failed"])
        self.assertIn("condition", C.check(p, listing(condition="требует ремонта"))["failed"])

    def test_budget_tolerance(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="any", metro_any=True)
        self.assertEqual(C.status(p, listing(price=9_400_000)), "fits")       # +4,4% — можно сторговать
        self.assertEqual(C.status(p, listing(price=9_600_000)), "rejected")   # +6,7% — уже нет

    def test_wrong_rooms_and_city(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="any", metro_any=True)
        self.assertEqual(C.status(p, listing(rooms=3)), "rejected")
        self.assertEqual(C.status(p, listing(city="Москва")), "rejected")


class FeaturesTest(unittest.TestCase):
    """Кейс с лоджией: нет данных — показываем с пометкой, явное «без» — скрываем."""

    def setUp(self):
        self.p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                         condition="any", metro_any=True, features=["лоджия"])

    def test_no_info_about_feature_is_shown_with_warning(self):
        x = listing(description="Светлая квартира, окна во двор")
        res = C.check(self.p, x)
        self.assertEqual(res["failed"], [])
        self.assertEqual(res["unknown"], ["features"])
        self.assertEqual(C.status(self.p, x), "fits")
        self.assertEqual(C.unknown_labels(self.p, x), ["лоджия/балкон"])

    def test_feature_mentioned_is_confirmed(self):
        x = listing(description="Квартира с лоджией, окна во двор")
        self.assertEqual(C.check(self.p, x)["unknown"], [])
        self.assertEqual(C.status(self.p, x), "fits")

    def test_explicit_no_feature_is_rejected(self):
        x = listing(description="Квартира без лоджии, окна во двор")
        self.assertEqual(C.check(self.p, x)["failed"], ["features"])
        self.assertEqual(C.status(self.p, x), "rejected")


class SelectTest(unittest.TestCase):
    def test_rejected_never_shown(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="ready", metro_max_minutes=10)
        good = listing(id="good", condition="евроремонт", metro={"minutes": 5, "mode": "пешком"})
        bad = listing(id="bad", condition="требует ремонта", metro={"minutes": 40, "mode": "транспортом"})
        sel, info = C.select(p, [good, bad], limit=10)
        self.assertEqual([x["id"] for x in sel], ["good"])
        self.assertEqual(info["rejected"], 1)

    def test_unverified_fills_the_gap(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="cosmetic", metro_max_minutes=10, area_min=40)
        unclear = listing(id="unclear", condition=None, metro=None, area=None)
        sel, info = C.select(p, [unclear], limit=10)
        self.assertEqual([x["id"] for x in sel], ["unclear"])
        self.assertEqual(info["confirmed"], 0)
        self.assertEqual(info["unverified"], 1)

    def test_rank_prefers_confirmed(self):
        p = profile(city="Казань", rooms=2, budget_max=9_000_000,
                    condition="ready", metro_max_minutes=10, area_min=40)
        clear = listing(id="clear", condition="евроремонт", metro={"minutes": 5, "mode": "пешком"})
        unclear = listing(id="unclear", condition=None, metro=None, area=None)
        self.assertEqual([x["id"] for x in C.rank(p, [unclear, clear])], ["clear", "unclear"])


class ParseTest(unittest.TestCase):
    def test_rules_parse_new_criteria(self):
        p = T.parse_request_rules(
            "Двушка в Казани до 9 млн, косметический ремонт, не первый и не последний этаж, "
            "метро до 10 минут, кирпич, не старше 1990, рядом школа, срочно, обязательно лоджия"
        )
        self.assertEqual(p["condition"], "cosmetic")
        self.assertTrue(p["not_first_floor"])
        self.assertTrue(p["not_last_floor"])
        self.assertEqual(p["metro_max_minutes"], 10)
        self.assertIn("кирпич", p["material"])
        self.assertEqual(p["year_min"], 1990)
        self.assertEqual(p["infra"], ["школа"])
        self.assertEqual(p["timeline"], "month")
        self.assertEqual(p["features"], ["лоджия"])

    def test_summary_mentions_criteria(self):
        p = T.merge_profile({"city": "Казань", "rooms": 2, "budget_max": 9_000_000,
                             "condition": "cosmetic", "metro_max_minutes": 10,
                             "features": ["лоджия"]})
        text = T.profile_summary(p)
        self.assertIn("Казань", text)
        self.assertIn("косметический", text)
        self.assertIn("метро", text)
        self.assertIn("лоджия", text)


if __name__ == "__main__":
    unittest.main()
