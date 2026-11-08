"""Диапазон цены: нижняя граница −30% от максимума и фильтрация снизу."""
import unittest

from backend.agent.profile import (default_budget_min, merge_profile, passes_hard_filters,
                                   profile_summary)


class BudgetRangeTest(unittest.TestCase):
    def test_default_low_is_minus_30_percent(self):
        self.assertEqual(default_budget_min(30_000_000), 21_000_000)
        self.assertEqual(default_budget_min(9_000_000), 6_300_000)
        self.assertEqual(default_budget_min(1_000_000), 700_000)
        self.assertIsNone(default_budget_min(None))

    def test_default_rounds_to_hundred_thousands(self):
        self.assertEqual(default_budget_min(8_760_000) % 100_000, 0)

    def test_price_below_low_bound_is_rejected(self):
        p = merge_profile({"city": "Казань", "rooms": 2, "budget_max": 30_000_000,
                           "budget_min": default_budget_min(30_000_000)})
        self.assertFalse(passes_hard_filters({"city": "Казань", "rooms": 2, "price": 15_000_000}, p))
        self.assertTrue(passes_hard_filters({"city": "Казань", "rooms": 2, "price": 21_000_000}, p))
        self.assertTrue(passes_hard_filters({"city": "Казань", "rooms": 2, "price": 25_000_000}, p))

    def test_client_low_bound_is_kept(self):
        p = merge_profile({"budget_max": 30_000_000, "budget_min": 10_000_000})
        self.assertEqual(p["budget_min"], 10_000_000)

    def test_summary_mentions_both_bounds(self):
        p = merge_profile({"city": "Казань", "rooms": 2, "budget_max": 30_000_000,
                           "budget_min": 21_000_000})
        text = profile_summary(p)
        self.assertIn("21 000 000", text)
        self.assertIn("30 000 000", text)
        self.assertIn("от", text)

    def test_summary_without_low_bound(self):
        p = merge_profile({"city": "Казань", "rooms": 2, "budget_max": 30_000_000})
        self.assertIn("до 30 000 000", profile_summary(p))


if __name__ == "__main__":
    unittest.main()
