"""
Binance DCA bot - achat mensuel automatique + rééquilibrage.

Sans dépendance externe : il suffit de Python 3.9+ (bibliothèque standard).

Commandes :
    python bot.py check    -> vérifie la clé API, les permissions et les paires
    python bot.py status   -> affiche le portefeuille actuel
    python bot.py run      -> exécution normale (à lancer chaque jour par la tâche planifiée)

Sécurité :
    - Le bot REFUSE de démarrer si ta clé API autorise les retraits.
    - Il ne fait JAMAIS de retrait (aucun code de retrait n'existe dans ce fichier).
    - Mode simulation (dry_run) activé par défaut : aucun ordre réel.
"""

import csv
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")
LOG_DIR = os.path.join(BASE_DIR, "logs")
JOURNAL_PATH = os.path.join(BASE_DIR, "journal.csv")

log = logging.getLogger("dca")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_env(path=ENV_PATH):
    """Lit le fichier .env (BINANCE_API_KEY=..., BINANCE_API_SECRET=...)."""
    values = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if os.environ.get(k):
            values[k] = os.environ[k]
    return values


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    alloc = cfg["allocation"]
    total = sum(alloc.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"La répartition doit faire 100 % (actuellement {total*100:.1f} %).")
    if cfg["monthly_budget"] <= 0:
        raise ValueError("monthly_budget doit être positif.")
    if cfg["monthly_budget"] > cfg.get("max_spend_per_run", cfg["monthly_budget"]):
        raise ValueError("monthly_budget dépasse max_spend_per_run.")
    return cfg


# ---------------------------------------------------------------------------
# Client Binance minimal (lecture + ordres spot, PAS de retrait)
# ---------------------------------------------------------------------------

class BinanceError(Exception):
    pass


class BinanceClient:
    def __init__(self, api_key, api_secret, base_url="https://api.binance.com", timeout=20):
        self.api_key = api_key
        self.api_secret = (api_secret or "").encode()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._rules_cache = {}

    def _request(self, method, path, params=None, signed=False):
        params = dict(params or {})
        headers = {"User-Agent": "dca-bot/1.0"}
        if signed:
            if not self.api_key or not self.api_secret:
                raise BinanceError("Clé API manquante : remplis le fichier .env")
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 10000
            query = urllib.parse.urlencode(params)
            sig = hmac.new(self.api_secret, query.encode(), hashlib.sha256).hexdigest()
            query += f"&signature={sig}"
            headers["X-MBX-APIKEY"] = self.api_key
        else:
            query = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}"
        data = None
        if method == "GET":
            if query:
                url += "?" + query
        else:
            data = query.encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise BinanceError(f"HTTP {e.code} sur {path} : {body}") from None
        except urllib.error.URLError as e:
            raise BinanceError(f"Connexion impossible à Binance : {e.reason}") from None

    # --- lecture ---
    def api_restrictions(self):
        return self._request("GET", "/sapi/v1/account/apiRestrictions", signed=True)

    def balances(self):
        acc = self._request("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)
        return {b["asset"]: Decimal(b["free"]) for b in acc["balances"] if Decimal(b["free"]) > 0}

    def price(self, symbol):
        return Decimal(self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})["price"])

    def symbol_rules(self, symbol):
        if symbol in self._rules_cache:
            return self._rules_cache[symbol]
        info = self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
        s = info["symbols"][0]
        if s.get("status") != "TRADING":
            raise BinanceError(f"La paire {symbol} n'est pas disponible au trading.")
        rules = {"step": Decimal("0.00000001"), "min_qty": Decimal("0"), "min_notional": Decimal("5")}
        for f in s["filters"]:
            if f["filterType"] in ("LOT_SIZE", "MARKET_LOT_SIZE") and Decimal(f["stepSize"]) > 0:
                rules["step"] = max(rules["step"], Decimal(f["stepSize"]))
                rules["min_qty"] = max(rules["min_qty"], Decimal(f["minQty"]))
            if f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
                rules["min_notional"] = Decimal(f.get("minNotional", "5"))
        self._rules_cache[symbol] = rules
        return rules

    # --- ordres (au marché) ---
    def market_buy(self, symbol, quote_amount, test=False):
        path = "/api/v3/order/test" if test else "/api/v3/order"
        params = {"symbol": symbol, "side": "BUY", "type": "MARKET",
                  "quoteOrderQty": f"{quote_amount:f}", "newOrderRespType": "FULL"}
        return self._request("POST", path, params, signed=True)

    def market_sell(self, symbol, quantity, test=False):
        path = "/api/v3/order/test" if test else "/api/v3/order"
        params = {"symbol": symbol, "side": "SELL", "type": "MARKET",
                  "quantity": f"{quantity:f}", "newOrderRespType": "FULL"}
        return self._request("POST", path, params, signed=True)


# ---------------------------------------------------------------------------
# Logique de portefeuille (fonctions pures, testées)
# ---------------------------------------------------------------------------

def floor_step(qty, step):
    if step <= 0:
        return qty
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def portfolio_values(holdings, prices):
    """holdings: {asset: qty}, prices: {asset: prix en monnaie de cotation}."""
    return {a: holdings.get(a, Decimal(0)) * prices[a] for a in prices}


def split_monthly_buy(budget, values, allocation, min_notional):
    """
    Répartit le budget mensuel en priorité vers les cryptos sous-pondérées.
    Cela rééquilibre en douceur, sans vendre.
    Retourne {asset: montant à acheter}.
    """
    budget = Decimal(budget)
    total_after = sum(values.values()) + budget
    needs = {a: max(Decimal(0), total_after * Decimal(str(w)) - values.get(a, Decimal(0)))
             for a, w in allocation.items()}
    need_sum = sum(needs.values())
    if need_sum > 0:
        split = {a: budget * n / need_sum for a, n in needs.items()}
    else:
        split = {a: budget * Decimal(str(w)) for a, w in allocation.items()}

    # Les montants trop petits pour Binance sont reportés sur les autres
    for _ in range(len(split)):
        small = [a for a, v in split.items() if 0 < v < min_notional.get(a, Decimal(0))]
        if not small:
            break
        for a in small:
            leftover = split.pop(a)
            if not split:
                return {}
            biggest = max(split, key=lambda k: (needs.get(k, 0), split[k]))
            split[biggest] += leftover
    return {a: v.quantize(Decimal("0.01"), rounding=ROUND_DOWN) for a, v in split.items() if v > 0}


def needs_rebalance(values, allocation, threshold):
    total = sum(values.values())
    if total <= 0:
        return False
    return any(abs(values.get(a, 0) / total - Decimal(str(w))) > Decimal(str(threshold))
               for a, w in allocation.items())


def rebalance_orders(values, prices, allocation, min_notional):
    """
    Calcule les ventes (des cryptos sur-pondérées) et les achats (des sous-pondérées)
    pour revenir à la répartition cible. Retourne (ventes {asset: qty}, achats {asset: montant}).
    """
    total = sum(values.values())
    sells, buys = {}, {}
    for a, w in allocation.items():
        diff = values.get(a, Decimal(0)) - total * Decimal(str(w))
        if diff > 0 and diff >= min_notional.get(a, Decimal(0)):
            sells[a] = diff / prices[a]
        elif diff < 0 and -diff >= min_notional.get(a, Decimal(0)):
            buys[a] = -diff
    return sells, buys


# ---------------------------------------------------------------------------
# État, journal, notifications
# ---------------------------------------------------------------------------

def state_path(dry_run):
    return os.path.join(BASE_DIR, "state_simulation.json" if dry_run else "state.json")


def load_state(dry_run):
    p = state_path(dry_run)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"last_buy_month": None, "last_rebalance": None, "goal_notified": False}


def save_state(state, dry_run):
    p = state_path(dry_run)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, p)


def journal(mode, side, symbol, qty, quote_amount, price, reason, order_id=""):
    new = not os.path.exists(JOURNAL_PATH)
    with open(JOURNAL_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(["date", "mode", "sens", "paire", "quantite", "montant", "prix", "raison", "id_ordre"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), mode, side, symbol,
                    f"{qty:f}", f"{quote_amount:.2f}", f"{price:f}", reason, order_id])


def history_path():
    return os.path.join(BASE_DIR, "history.csv")


def record_history(now, values, cash):
    """Une ligne par jour avec la valeur réelle du portefeuille (pour le graphique)."""
    p = history_path()
    day = now.strftime("%Y-%m-%d")
    rows = []
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            rows = [r for r in csv.reader(f, delimiter=";")][1:]
    rows = [r for r in rows if r and r[0] != day]
    assets = sorted(values)
    rows.append([day, f"{sum(values.values()):.2f}", f"{cash:.2f}"] + [f"{values[a]:.2f}" for a in assets])
    tmp = p + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["date", "total", "liquidites"] + assets)
        w.writerows(rows)
    os.replace(tmp, p)


def notify(env, text):
    log.info("NOTIFICATION : %s", text)
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=15)
    except Exception as e:  # une notification ratée ne doit jamais bloquer le bot
        log.warning("Notification Telegram impossible : %s", e)


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class Bot:
    def __init__(self, cfg, client, env, now=None):
        self.cfg = cfg
        self.client = client
        self.env = env
        self.now = now or datetime.now()
        self.dry = bool(cfg.get("dry_run", True))
        self.quote = cfg["quote_asset"]
        self.alloc = cfg["allocation"]
        self.mode = "SIMULATION" if self.dry else "REEL"

    def sym(self, asset):
        return f"{asset}{self.quote}"

    def safety_check(self):
        r = self.client.api_restrictions()
        if r.get("enableWithdrawals"):
            raise BinanceError("ARRÊT : ta clé API autorise les RETRAITS. Désactive cette permission sur Binance.")
        if r.get("enableInternalTransfer") or r.get("permitsUniversalTransfer"):
            raise BinanceError("ARRÊT : ta clé API autorise les transferts. Désactive cette permission sur Binance.")
        if not r.get("enableSpotAndMarginTrading") and not self.dry:
            raise BinanceError("Ta clé API n'a pas le droit de trader en spot (active 'Enable Spot & Margin Trading').")
        if not r.get("ipRestrict"):
            log.warning("Conseil : ta clé API n'est pas limitée à l'adresse IP de ta machine.")
        return r

    def snapshot(self):
        balances = self.client.balances()
        prices = {a: self.client.price(self.sym(a)) for a in self.alloc}
        holdings = {a: balances.get(a, Decimal(0)) for a in self.alloc}
        values = portfolio_values(holdings, prices)
        cash = balances.get(self.quote, Decimal(0))
        return holdings, prices, values, cash

    def min_notional(self):
        return {a: self.client.symbol_rules(self.sym(a))["min_notional"] for a in self.alloc}

    def _buy(self, asset, amount, reason):
        sym = self.sym(asset)
        price = self.client.price(sym)
        res = self.client.market_buy(sym, amount, test=self.dry)
        qty = Decimal(res.get("executedQty", "0")) if not self.dry else amount / price
        spent = Decimal(res.get("cummulativeQuoteQty", "0")) if not self.dry else amount
        log.info("[%s] ACHAT %s pour %s %s (~%s %s)", self.mode, sym, f"{spent:.2f}", self.quote, f"{qty:.8f}", asset)
        journal(self.mode, "ACHAT", sym, qty, spent, price, reason, res.get("orderId", ""))
        return spent

    def _sell(self, asset, qty, reason):
        sym = self.sym(asset)
        rules = self.client.symbol_rules(sym)
        qty = floor_step(qty, rules["step"])
        if qty < rules["min_qty"] or qty <= 0:
            log.info("Vente %s ignorée (quantité trop petite).", sym)
            return Decimal(0)
        price = self.client.price(sym)
        res = self.client.market_sell(sym, qty, test=self.dry)
        got = Decimal(res.get("cummulativeQuoteQty", "0")) if not self.dry else qty * price
        log.info("[%s] VENTE %s %s %s (~%s %s)", self.mode, f"{qty:.8f}", asset, sym, f"{got:.2f}", self.quote)
        journal(self.mode, "VENTE", sym, qty, got, price, reason, res.get("orderId", ""))
        return got

    def monthly_buy(self, state, values, cash):
        month = self.now.strftime("%Y-%m")
        if state.get("last_buy_month") == month:
            log.info("Achat mensuel déjà fait pour %s.", month)
            return values, False
        if self.now.day < int(self.cfg.get("buy_day", 1)):
            log.info("Achat mensuel prévu à partir du %s du mois.", self.cfg.get("buy_day", 1))
            return values, False

        budget = Decimal(str(self.cfg["monthly_budget"]))
        if not self.dry and cash < budget:
            if cash < min(self.min_notional().values()):
                notify(self.env, f"Achat mensuel impossible : solde {self.quote} insuffisant ({cash:.2f}). "
                                 f"Dépose et convertis tes {self.cfg['monthly_budget']} sur Binance.")
                return values, False
            log.warning("Solde %s (%.2f) inférieur au budget : achat avec ce qui est disponible.", self.quote, cash)
            budget = cash.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        split = split_monthly_buy(budget, values, self.alloc, self.min_notional())
        spent_total = Decimal(0)
        for asset, amount in split.items():
            spent = self._buy(asset, amount, "achat mensuel")
            spent_total += spent
            values[asset] = values.get(asset, Decimal(0)) + spent
        state["last_buy_month"] = month
        notify(self.env, f"[{self.mode}] Achat mensuel fait : "
               + ", ".join(f"{a} {v:.2f} {self.quote}" for a, v in split.items()))
        return values, True

    def maybe_rebalance(self, state, values, prices):
        if not needs_rebalance(values, self.alloc, self.cfg["rebalance_threshold"]):
            return False
        last = state.get("last_rebalance")
        cooldown = timedelta(days=int(self.cfg.get("rebalance_cooldown_days", 30)))
        if last and self.now - datetime.fromisoformat(last) < cooldown:
            log.info("Rééquilibrage nécessaire mais attente du délai minimum (%s jours).", cooldown.days)
            return False
        if sum(values.values()) < Decimal(str(self.cfg.get("min_portfolio_for_rebalance", 100))):
            log.info("Portefeuille trop petit pour rééquilibrer, les achats mensuels s'en chargent.")
            return False

        sells, buys = rebalance_orders(values, prices, self.alloc, self.min_notional())
        if not sells or not buys:
            return False
        proceeds = Decimal(0)
        for asset, qty in sells.items():
            proceeds += self._sell(asset, qty, "rééquilibrage")
        # On ne réachète qu'avec l'argent obtenu par les ventes (marge de 0,5 % pour les frais)
        budget = proceeds * Decimal("0.995")
        want = sum(buys.values())
        for asset, amount in buys.items():
            part = (budget * amount / want).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if part >= self.min_notional()[asset]:
                self._buy(asset, part, "rééquilibrage")
        state["last_rebalance"] = self.now.isoformat(timespec="seconds")
        notify(self.env, f"[{self.mode}] Rééquilibrage effectué vers "
               + ", ".join(f"{a} {w*100:.0f} %" for a, w in self.alloc.items()))
        return True

    def check_goal(self, state, values):
        goal = Decimal(str(self.cfg.get("goal_value", 0)))
        total = sum(values.values())
        if goal > 0 and total >= goal and not state.get("goal_notified"):
            notify(self.env, f"Objectif atteint : ton portefeuille vaut {total:.2f} {self.quote} "
                             f"(objectif {goal:.0f}). Tu peux envisager un retrait manuel depuis Binance.")
            state["goal_notified"] = True
        elif goal > 0 and total < goal * Decimal("0.9"):
            state["goal_notified"] = False

    def run(self):
        log.info("===== Exécution %s =====", self.mode)
        self.safety_check()
        state = load_state(self.dry)
        holdings, prices, values, cash = self.snapshot()
        values, _ = self.monthly_buy(state, values, cash)
        if not self.dry:
            holdings, prices, values, cash = self.snapshot()
        self.maybe_rebalance(state, values, prices)
        self.check_goal(state, values)
        state["last_run"] = self.now.isoformat(timespec="seconds")
        save_state(state, self.dry)
        log.info("Valeur totale : %.2f %s", sum(values.values()), self.quote)
        try:
            _, _, real_values, real_cash = self.snapshot()
            record_history(self.now, real_values, real_cash)
        except Exception as e:
            log.warning("Historique non enregistré : %s", e)

    def status(self):
        holdings, prices, values, cash = self.snapshot()
        total = sum(values.values())
        print(f"Mode : {self.mode}")
        print(f"Liquidités : {cash:.2f} {self.quote}")
        for a in self.alloc:
            share = (values[a] / total * 100) if total else 0
            print(f"{a:>5} : {holdings[a]:.8f}  = {values[a]:.2f} {self.quote}  "
                  f"({share:.1f} % / cible {self.alloc[a]*100:.0f} %)")
        print(f"Total cryptos : {total:.2f} {self.quote}  (objectif {self.cfg.get('goal_value')})")


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(os.path.join(LOG_DIR, "bot.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.handlers = [fh, sh]


def main(argv):
    setup_logging()
    cmd = argv[1] if len(argv) > 1 else "run"
    try:
        cfg = load_config()
        env = load_env()
        client = BinanceClient(env.get("BINANCE_API_KEY"), env.get("BINANCE_API_SECRET"),
                               cfg.get("base_url", "https://api.binance.com"))
        bot = Bot(cfg, client, env)
        if cmd == "check":
            r = bot.safety_check()
            for a in bot.alloc:
                client.symbol_rules(bot.sym(a))
            print("OK : clé API valide, retraits désactivés, paires disponibles.")
            print(f"Restriction IP : {'oui' if r.get('ipRestrict') else 'NON (conseillé de l activer)'}")
            print(f"Mode actuel : {bot.mode}")
        elif cmd == "status":
            bot.status()
        elif cmd == "run":
            bot.run()
        else:
            print(__doc__)
            return 2
        return 0
    except Exception as e:
        log.error("ERREUR : %s", e)
        try:
            notify(load_env(), f"Le bot DCA a rencontré une erreur : {e}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
