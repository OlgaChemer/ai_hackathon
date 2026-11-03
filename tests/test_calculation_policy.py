"""Согласованность расчётов и сохранённых сессий при обновлении демо-политики."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from backend import calculation_policy as policy, db
from backend.agent import finance, mortgage, qa, session
from backend.agent.actions import money
from backend.agent.profile import merge_profile
from tests.fakes import ScriptedLLM


class CalculationPolicyTest(unittest.IsolatedAsyncioTestCase):
    def test_invalid_configuration_is_rejected(self):
        invalid = []
        value = deepcopy(policy.POLICY)
        value["savings"]["profiles"]["moderate"]["mix"]["deposit"] = -0.5
        invalid.append(value)
        value = deepcopy(policy.POLICY)
        value["mortgage"]["subsidy_years"] = value["mortgage"]["years"]
        invalid.append(value)
        value = deepcopy(policy.POLICY)
        value["savings"]["products"]["savings_account"]["rate"] = float("nan")
        invalid.append(value)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    policy.validate(value)

    def test_cash_projection_uses_the_product_rate_instead_of_a_ui_constant(self):
        initial = finance.cash_preview(12_000_000)
        with patch.dict(finance.PRODUCTS["savings_account"], rate=initial["rate"] / 2):
            changed = finance.cash_preview(12_000_000)
            self.assertEqual(changed["income"], initial["income"] // 2)
            self.assertEqual(changed["rate"], initial["rate"] / 2)
            self.assertEqual(changed["assumptions"], policy.metadata())

    def test_qa_and_mortgage_use_the_same_policy(self):
        db.configure("sqlite://")
        s = session.create_session("", ScriptedLLM())
        context = qa.build_context(s)["допущения расчёта"]
        result = mortgage.programs(
            9_000_000, "Казань", {"children": 1, "youngest_age": 4}
        )
        self.assertEqual(
            context["ипотека"]["family_scale"]["regions"]["1"][0],
            result["programs"][1]["rate"],
        )
        self.assertEqual(context["fingerprint"], result["assumptions"]["fingerprint"])

    async def test_updated_policy_invalidates_persisted_finance_but_keeps_approved_limit(
        self,
    ):
        db.configure("sqlite://")
        s = session.create_session("", ScriptedLLM())
        s.profile = merge_profile(
            {"city": "Казань", "rooms": 2, "budget_max": 9_000_000, "payment": "cash"}
        )
        s.red_lines_ok = True
        s.profile["_approved_budget_max"] = 9_000_000
        await money.mortgage_programs(s)
        s.remember()
        with patch.object(finance, "FINGERPRINT", "updated-policy"):
            restored = session.restore_session(s.id, "Продолжить", s.llm)
            self.assertIsNone(restored.programs_result)
            self.assertTrue(restored.red_lines_ok)
