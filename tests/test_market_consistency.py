"""Область рынка, реальные источники и единые числовые метрики."""
from copy import deepcopy
from datetime import datetime,timezone
import unittest
from unittest.mock import AsyncMock,patch

from backend import db,sources
from backend.agent.profile import merge_profile
from backend.market_statistics import prices
from backend.sources import market_estimate as M
from backend.workshop import data
from tests.test_llm_and_agent import listing


class MarketConsistency(unittest.IsolatedAsyncioTestCase):
    def setUp(self):db.configure('sqlite://')

    async def test_soft_center_is_not_a_hidden_cao_or_radius_filter(self):
        profile=merge_profile({'city':'Москва','rooms':3,'prefer_center':True,'housing':'both'})
        before=deepcopy(profile);collected=[]
        async def fetch(city,rooms,budget,page,okrug,new):
            self.assertIsNone(okrug)
            rows=[listing(id=f'{new}-{i}',city=city,rooms=rooms,is_new=new,
                lat=55.6,lng=37.6,price=20_000_000+i*100_000+int(new)*10_000_000) for i in range(6)]
            collected.extend(rows)
            return rows,{'status':'live'}
        with patch.object(sources,'stored',return_value=[]),patch.object(sources.cian,'search',AsyncMock(side_effect=fetch)):
            result=await M.estimate(profile)
        self.assertIsNone(result['scope']['center_radius_km'])
        self.assertEqual(result['count'],12)
        workshop=data.facts({'kind':'market','listings':collected,'assumptions':{}})
        self.assertEqual(result['median'],workshop['median_price'])
        self.assertEqual(result['ppm2'],workshop['median_price_per_m2'])
        self.assertEqual(result['min'],workshop['min_price'])
        self.assertEqual(profile,before)

    async def test_recent_saved_listings_are_not_lost_behind_a_few_luxury_offers(self):
        profile=merge_profile({'city':'Москва','rooms':3,'housing':'both','prefer_center':True})
        known=[listing(id=f'known-{i}',city='Москва',rooms=3,is_new=bool(i%2),price=20_000_000+i*100_000) for i in range(30)]
        luxury=[listing(id='luxury',city='Москва',rooms=3,price=1_300_000_000,area=1000)]
        with patch.object(sources,'stored',return_value=known),patch.object(sources.cian,'search',AsyncMock(return_value=(luxury,{'status':'live'}))):
            result=await M.estimate(profile)
        self.assertEqual(result['count'],31)
        self.assertEqual(result['median'],prices(known+luxury)['median'])
        self.assertLess(result['median'],30_000_000)
        self.assertGreater(result['mean'],result['median'])
        self.assertIn('recent_saved_listings',result['reasons'])

    def test_explicit_center_keeps_coordinate_check_without_forcing_an_okrug(self):
        profile=merge_profile({'city':'Москва','rooms':3,'center_required':True})
        scope=M.scope(profile)
        self.assertEqual(scope['center_radius_km'],5)
        self.assertIsNone(scope['okrug'])
        self.assertFalse(M.matches(listing(city='Москва',rooms=3,lat=55.4,lng=37.6),scope))
        self.assertIsNone(M.matches(listing(city='Москва',rooms=3),scope))
        self.assertTrue(M.matches(listing(city='Москва',rooms=3,lat=55.755,lng=37.62),scope))

    def test_budget_range_and_apartments_have_the_same_meaning_in_research(self):
        profile=merge_profile({'city':'Москва','rooms':3,'budget_max':27_000_000,'budget_min':18_900_000,
            'apartments':'hide'})
        profile['_research_query']={'respect_budget':True,'group_by':None,'aggregates':[]}
        scope=M.scope(profile)
        self.assertFalse(M.matches(listing(city='Москва',rooms=3,price=10_000_000),scope))
        self.assertFalse(M.matches(listing(city='Москва',rooms=3,price=25_000_000,is_apartments=True),scope))
        self.assertTrue(M.matches(listing(city='Москва',rooms=3,price=25_000_000),scope))

    def test_cache_from_previous_sampling_policy_is_invalid(self):
        profile=merge_profile({'city':'Москва','rooms':3,'prefer_center':True})
        old={'profile_key':'old-cao-sample','generated_at':datetime.now(timezone.utc).isoformat()}
        self.assertFalse(M.current(old,profile))
        self.assertNotEqual(M.key(profile),M.key({**profile,'center_required':True}))
