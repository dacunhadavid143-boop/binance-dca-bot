"""Tests hors-ligne avec un faux Binance : python tests.py"""
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal as D

import bot


class FakeClient:
    def __init__(self, balances, prices, restrictions=None):
        self.bal = {k: D(str(v)) for k, v in balances.items()}
        self.prices = {k: D(str(v)) for k, v in prices.items()}
        self.restr = restrictions or {"enableWithdrawals": False, "enableSpotAndMarginTrading": True, "ipRestrict": True}
        self.orders = []

    def api_restrictions(self): return self.restr
    def balances(self): return {k: v for k, v in self.bal.items() if v > 0}
    def price(self, sym): return self.prices[sym]
    def symbol_rules(self, sym): return {"step": D("0.00001"), "min_qty": D("0.00001"), "min_notional": D("5")}

    def market_buy(self, sym, amount, test=False):
        self.orders.append(("BUY", sym, amount, test))
        if test: return {}
        asset = sym.replace("USDC", "")
        qty = amount / self.prices[sym]
        self.bal["USDC"] -= amount
        self.bal[asset] = self.bal.get(asset, D(0)) + qty
        return {"executedQty": str(qty), "cummulativeQuoteQty": str(amount), "orderId": len(self.orders)}

    def market_sell(self, sym, qty, test=False):
        self.orders.append(("SELL", sym, qty, test))
        if test: return {}
        asset = sym.replace("USDC", "")
        got = qty * self.prices[sym]
        self.bal[asset] -= qty
        self.bal["USDC"] += got
        return {"executedQty": str(qty), "cummulativeQuoteQty": str(got), "orderId": len(self.orders)}


CFG = {"dry_run": False, "quote_asset": "USDC", "monthly_budget": 50, "max_spend_per_run": 60, "buy_day": 2,
       "allocation": {"BTC": 0.7, "ETH": 0.3}, "rebalance_threshold": 0.10, "rebalance_cooldown_days": 30,
       "min_portfolio_for_rebalance": 100, "goal_value": 2000}
PRICES = {"BTCUSDC": 100000, "ETHUSDC": 4000}


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        bot.BASE_DIR = self.tmp
        bot.JOURNAL_PATH = os.path.join(self.tmp, "journal.csv")
        bot.log.handlers = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def mk(self, balances, now=datetime(2026, 11, 3), **over):
        cfg = dict(CFG, **over)
        c = FakeClient(balances, PRICES)
        return bot.Bot(cfg, c, {}, now=now), c

    def test_premier_achat_70_30(self):
        b, c = self.mk({"USDC": 50})
        b.run()
        buys = {o[1]: o[2] for o in c.orders if o[0] == "BUY"}
        self.assertEqual(buys, {"BTCUSDC": D("35.00"), "ETHUSDC": D("15.00")})

    def test_un_seul_achat_par_mois(self):
        b, c = self.mk({"USDC": 200})
        b.run(); b.run(); b.run()
        self.assertEqual(len([o for o in c.orders if o[0] == "BUY"]), 2)

    def test_mois_suivant_rachete(self):
        b, c = self.mk({"USDC": 200})
        b.run()
        b2 = bot.Bot(dict(CFG), c, {}, now=datetime(2026, 12, 2))
        b2.run()
        self.assertEqual(len([o for o in c.orders if o[0] == "BUY"]), 4)

    def test_avant_jour_achat_rien(self):
        b, c = self.mk({"USDC": 50}, now=datetime(2026, 11, 1))
        b.run()
        self.assertEqual(c.orders, [])

    def test_refuse_si_retraits_autorises(self):
        b, c = self.mk({"USDC": 50})
        c.restr["enableWithdrawals"] = True
        with self.assertRaises(bot.BinanceError):
            b.run()
        self.assertEqual(c.orders, [])

    def test_achat_oriente_vers_sous_pondere(self):
        # 300 en ETH, 700 en BTC -> parfait ; mais BTC chute : 500 BTC / 300 ETH
        b, c = self.mk({"USDC": 50, "BTC": 0.005, "ETH": 0.075})
        b.run()
        buys = {o[1]: o[2] for o in c.orders if o[0] == "BUY"}
        self.assertEqual(buys, {"BTCUSDC": D("50.00")})  # tout va vers BTC
        self.assertFalse(any(o[0] == "SELL" for o in c.orders))

    def test_reequilibrage_vend_et_rachete(self):
        # 400 BTC / 600 ETH -> 40 % au lieu de 70 % : écart > 10 pts
        b, c = self.mk({"USDC": 0, "BTC": 0.004, "ETH": 0.15})
        b.run()
        sells = [o for o in c.orders if o[0] == "SELL"]
        self.assertEqual(sells[0][1], "ETHUSDC")
        vals = {a: c.bal[a] * PRICES[a + "USDC"] for a in ("BTC", "ETH")}
        share = vals["BTC"] / sum(vals.values())
        self.assertAlmostEqual(float(share), 0.70, delta=0.02)
        self.assertGreaterEqual(c.bal["USDC"], 0)

    def test_reequilibrage_delai(self):
        b, c = self.mk({"USDC": 0, "BTC": 0.004, "ETH": 0.15})
        b.run()
        n = len(c.orders)
        c.prices["ETHUSDC"] = D(8000)  # ETH double
        b2 = bot.Bot(dict(CFG), c, {}, now=datetime(2026, 11, 10))
        b2.run()
        self.assertEqual(len(c.orders), n)  # pas de nouveau rééquilibrage avant 30 jours

    def test_simulation_aucun_ordre_reel(self):
        b, c = self.mk({"USDC": 50, "BTC": 0.004, "ETH": 0.15}, dry_run=True)
        b.run()
        self.assertTrue(c.orders)
        self.assertTrue(all(o[3] for o in c.orders))  # tous en mode test
        self.assertEqual(c.bal["USDC"], 50)

    def test_solde_insuffisant(self):
        b, c = self.mk({"USDC": 3})
        b.run()
        self.assertEqual(c.orders, [])

    def test_petit_montant_reporte(self):
        split = bot.split_monthly_buy(D(10), {"BTC": D(0), "ETH": D(0)}, {"BTC": 0.7, "ETH": 0.3},
                                      {"BTC": D(5), "ETH": D(5)})
        self.assertTrue(all(v >= 5 for v in split.values()))
        self.assertEqual(sum(split.values()), D(10))

    def test_objectif(self):
        b, c = self.mk({"USDC": 50, "BTC": 0.02})  # 2000 en BTC
        b.run()
        self.assertTrue(bot.load_state(False)["goal_notified"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
