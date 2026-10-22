"""
Tableau de bord local du Binance DCA Bot.

    python app.py      -> ouvre http://127.0.0.1:8765 dans ton navigateur

Le tableau de bord n'est accessible que depuis cette machine (127.0.0.1).
Garde la fenêtre noire ouverte tant que tu l'utilises.
"""

import csv
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bot

PORT = int(os.environ.get("DCA_PORT", "8765"))
UI_PATH = os.path.join(bot.BASE_DIR, "ui", "index.html")
TASK_NAME = "Binance DCA Bot"
TOKEN = secrets.token_urlsafe(24)
RUN_LOCK = threading.Lock()
IS_WINDOWS = os.name == "nt"

# Permet aux tests de remplacer le client Binance
CLIENT_FACTORY = None


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def num(x):
    return float(x) if isinstance(x, Decimal) else x


def make_bot():
    cfg = bot.load_config(config_path())
    env = bot.load_env(env_path())
    if CLIENT_FACTORY:
        client = CLIENT_FACTORY(cfg, env)
    else:
        client = bot.BinanceClient(env.get("BINANCE_API_KEY"), env.get("BINANCE_API_SECRET"),
                                   cfg.get("base_url", "https://api.binance.com"))
    return bot.Bot(cfg, client, env), cfg, env


def config_path():
    return os.path.join(bot.BASE_DIR, "config.json")


def env_path():
    return os.path.join(bot.BASE_DIR, ".env")


def journal_path():
    return os.path.join(bot.BASE_DIR, "journal.csv")


def read_csv(path, limit=None):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    return rows[-limit:] if limit else rows


def next_buy_date(cfg, state, now):
    day = int(cfg.get("buy_day", 1))
    month = now.strftime("%Y-%m")
    if state.get("last_buy_month") == month:
        y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        return datetime(y, m, day).strftime("%Y-%m-%d")
    if now.day >= day:
        return "prochaine exécution"
    return datetime(now.year, now.month, day).strftime("%Y-%m-%d")


_schedule_cache = {"t": 0, "v": None}


def schedule_status(force=False):
    if not IS_WINDOWS:
        return {"supported": False, "installed": False, "next_run": None}
    if not force and _schedule_cache["v"] and time.time() - _schedule_cache["t"] < 60:
        return _schedule_cache["v"]
    cmd = ("$i = Get-ScheduledTaskInfo -TaskName '%s' -ErrorAction Stop; "
           "$i.NextRunTime.ToString('yyyy-MM-dd HH:mm')" % TASK_NAME)
    r = run_ps(["-Command", cmd])
    v = {"supported": True, "installed": r.returncode == 0,
         "next_run": r.stdout.strip() if r.returncode == 0 else None}
    _schedule_cache.update(t=time.time(), v=v)
    return v


def run_ps(args, timeout=60):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass"] + args,
                          capture_output=True, text=True, timeout=timeout, creationflags=flags)


def pythonw_path():
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def api_overview():
    now = datetime.now()
    out = {"now": now.isoformat(timespec="seconds")}
    try:
        cfg = bot.load_config(config_path())
    except Exception as e:
        return {"error": f"config.json invalide : {e}"}
    env = bot.load_env(env_path())
    key = env.get("BINANCE_API_KEY") or ""
    dry = bool(cfg.get("dry_run", True))
    state = bot.load_state(dry)
    out.update(
        config=cfg,
        mode="SIMULATION" if dry else "REEL",
        keys={"configured": bool(key and env.get("BINANCE_API_SECRET")),
              "hint": ("…" + key[-4:]) if len(key) >= 4 else ""},
        telegram=bool(env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")),
        state=state,
        next_buy=next_buy_date(cfg, state, now),
        schedule=schedule_status(),
    )
    p = {"ok": False}
    if out["keys"]["configured"] or CLIENT_FACTORY:
        try:
            b, _, _ = make_bot()
            holdings, prices, values, cash = b.snapshot()
            total = sum(values.values())
            p = {"ok": True, "cash": num(cash), "total": num(total), "assets": [
                {"asset": a, "qty": num(holdings[a]), "price": num(prices[a]), "value": num(values[a]),
                 "share": num(values[a] / total) if total else 0, "target": cfg["allocation"][a]}
                for a in cfg["allocation"]]}
        except Exception as e:
            p = {"ok": False, "error": str(e)}
    else:
        p["error"] = "Ajoute ta clé API dans l'onglet Réglages."
    out["portfolio"] = p
    return out


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(f"{record.levelname}: {record.getMessage()}")


def with_capture(fn):
    cap = Capture()
    bot.log.addHandler(cap)
    try:
        fn()
        return {"ok": True, "lines": cap.lines}
    except Exception as e:
        bot.log.error("ERREUR : %s", e)
        return {"ok": False, "error": str(e), "lines": cap.lines}
    finally:
        bot.log.removeHandler(cap)


def api_check():
    b, cfg, _ = make_bot()

    def go():
        r = b.safety_check()
        for a in b.alloc:
            b.client.symbol_rules(b.sym(a))
        bot.log.info("Clé API valide, retraits désactivés, paires %s disponibles.",
                     ", ".join(b.sym(a) for a in b.alloc))
        if not r.get("ipRestrict"):
            bot.log.warning("Conseil : limite ta clé API à l'adresse IP de cette machine.")
    return with_capture(go)


def api_run():
    if not RUN_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "Une exécution est déjà en cours."}
    try:
        b, _, _ = make_bot()
        return with_capture(b.run)
    finally:
        RUN_LOCK.release()


def api_save_config(data):
    with open(config_path(), encoding="utf-8") as f:
        cfg = json.load(f)
    if "dry_run" in data:
        cfg["dry_run"] = bool(data["dry_run"])
    if "monthly_budget" in data:
        budget = float(data["monthly_budget"])
        if not 5 <= budget <= 10000:
            raise ValueError("Le budget mensuel doit être entre 5 et 10 000.")
        cfg["monthly_budget"] = budget
        cfg["max_spend_per_run"] = round(budget * 1.2, 2)
    if "buy_day" in data:
        d = int(data["buy_day"])
        if not 1 <= d <= 28:
            raise ValueError("Le jour d'achat doit être entre 1 et 28.")
        cfg["buy_day"] = d
    if "allocation" in data:
        alloc = {a: round(float(v) / 100, 4) for a, v in data["allocation"].items()}
        if set(alloc) != set(cfg["allocation"]):
            raise ValueError("Cryptos inconnues dans la répartition.")
        if any(v < 0 for v in alloc.values()) or abs(sum(alloc.values()) - 1) > 0.0001:
            raise ValueError("La répartition doit faire exactement 100 %.")
        cfg["allocation"] = alloc
    if "rebalance_threshold" in data:
        t = float(data["rebalance_threshold"]) / 100
        if not 0.02 <= t <= 0.5:
            raise ValueError("Le seuil de rééquilibrage doit être entre 2 et 50 points.")
        cfg["rebalance_threshold"] = t
    if "goal_value" in data:
        g = float(data["goal_value"])
        if g < 0:
            raise ValueError("L'objectif ne peut pas être négatif.")
        cfg["goal_value"] = g
    tmp = config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    bot.load_config(tmp)  # validation complète avant d'écraser
    os.replace(tmp, config_path())
    return {"ok": True}


def api_save_keys(data):
    env = {}
    if os.path.exists(env_path()):
        env = bot.load_env(env_path())
    mapping = {"api_key": "BINANCE_API_KEY", "api_secret": "BINANCE_API_SECRET",
               "telegram_token": "TELEGRAM_BOT_TOKEN", "telegram_chat": "TELEGRAM_CHAT_ID"}
    for field, var in mapping.items():
        v = (data.get(field) or "").strip()
        if v:
            if any(c in v for c in "\r\n= \t"):
                raise ValueError(f"Valeur invalide pour {field}.")
            env[var] = v
    if data.get("clear_telegram"):
        env.pop("TELEGRAM_BOT_TOKEN", None)
        env.pop("TELEGRAM_CHAT_ID", None)
    lines = ["# Généré par le tableau de bord. Ne partage jamais ce fichier."]
    lines += [f"{k}={v}" for k, v in env.items() if v]
    tmp = env_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, env_path())
    return {"ok": True}


def api_schedule(data):
    if not IS_WINDOWS:
        return {"ok": False, "error": "L'automatisation se règle uniquement sous Windows."}
    if data.get("action") == "install":
        r = run_ps(["-File", os.path.join(bot.BASE_DIR, "installer_tache.ps1"), "-Python", pythonw_path()])
    else:
        r = run_ps(["-Command", f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false"])
    s = schedule_status(force=True)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout).strip()[:400], "schedule": s}
    return {"ok": True, "schedule": s}


def api_test_notification():
    env = bot.load_env(env_path())
    if not (env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")):
        return {"ok": False, "error": "Telegram n'est pas configuré."}
    bot.notify(env, "Test : les notifications du bot DCA fonctionnent.")
    return {"ok": True}


def api_logs():
    p = os.path.join(bot.LOG_DIR, "bot.log")
    if not os.path.exists(p):
        return {"lines": []}
    with open(p, encoding="utf-8", errors="replace") as f:
        return {"lines": f.readlines()[-200:]}


# ---------------------------------------------------------------------------
# Serveur HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "DCADashboard/1.0"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, default=num).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        path = self.path.split("?")[0]
        try:
            if path == "/":
                with open(UI_PATH, encoding="utf-8") as f:
                    html = f.read().replace("__TOKEN__", TOKEN)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if path == "/api/overview":
                return self._send(200, api_overview())
            if path == "/api/journal":
                return self._send(200, {"rows": read_csv(journal_path(), 200)})
            if path == "/api/history":
                return self._send(200, {"rows": read_csv(bot.history_path())})
            if path == "/api/logs":
                return self._send(200, api_logs())
            return self._send(404, {"error": "introuvable"})
        except Exception as e:
            return self._send(500, {"ok": False, "error": str(e)})

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Token") != TOKEN:
            return self._send(403, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"ok": False, "error": "Requête invalide"})
        routes = {
            "/api/check": lambda: api_check(),
            "/api/run": lambda: api_run(),
            "/api/config": lambda: api_save_config(data),
            "/api/keys": lambda: api_save_keys(data),
            "/api/schedule": lambda: api_schedule(data),
            "/api/test-notification": lambda: api_test_notification(),
        }
        fn = routes.get(self.path)
        if not fn:
            return self._send(404, {"error": "introuvable"})
        try:
            return self._send(200, fn())
        except Exception as e:
            return self._send(200, {"ok": False, "error": str(e)})


def main():
    bot.setup_logging()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print("=" * 56)
    print("  Tableau de bord du bot DCA")
    print(f"  Ouvert sur : {url}")
    print("  Laisse cette fenêtre ouverte. Ferme-la pour arrêter.")
    print("  (Le bot automatique continue de tourner sans elle.)")
    print("=" * 56)
    if not os.environ.get("DCA_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
