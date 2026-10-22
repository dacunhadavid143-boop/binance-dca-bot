# Binance DCA Bot

Chaque mois, le bot achète automatiquement du Bitcoin et de l'Ethereum (70 % / 30 %).
Si la répartition dérive de plus de 10 points (par ex. BTC sous 60 % ou au-dessus de 80 %),
il rééquilibre, au maximum une fois tous les 30 jours. Quand ton portefeuille atteint 2 000,
il te prévient : c'est toi qui décides de retirer, manuellement.

**Ce que le bot ne fait jamais** : retirer de l'argent, transférer des fonds, trader sur marge.
Il refuse même de démarrer si ta clé API autorise les retraits.

---

## Le plus simple : le tableau de bord

Double-clique sur **`lancer.bat`**. Une fenêtre noire s'ouvre (laisse-la ouverte), puis ton
navigateur affiche le tableau de bord sur `http://127.0.0.1:8765`. Il n'est accessible que depuis
cette machine.

Tout se fait de là : ajouter la clé API, vérifier la connexion, voir ton portefeuille,
l'évolution, la répartition, les transactions, modifier tes réglages, passer en mode réel et
activer l'automatisation (bouton « Activer » dans Réglages).

Le bot automatique tourne même quand le tableau de bord est fermé.

Les étapes ci-dessous détaillent la même chose pour ceux qui préfèrent la ligne de commande.

## 1. Installer Python sur la machine virtuelle

1. Télécharge Python sur https://www.python.org/downloads/
2. Pendant l'installation, **coche « Add Python to PATH »**.
3. Dézippe ce dossier, par exemple dans `C:\binance-dca-bot`.

Aucune autre installation n'est nécessaire.

## 2. Créer la clé API sur Binance

Binance → Profil → **Gestion des API** → Créer une API (type « générée par le système »).

Permissions à régler :

| Permission | Réglage |
|---|---|
| Enable Reading | ✅ activé |
| Enable Spot & Margin Trading | ✅ activé |
| Enable Withdrawals | ❌ **désactivé** |
| Enable Margin / Futures / Transferts | ❌ désactivé |
| Restrict access to trusted IPs | ✅ mets l'adresse IP de ta machine virtuelle |

Ensuite copie `.env.example` en `.env` et colle la clé et le secret dedans.
Ne partage jamais ce fichier, ni avec moi ni avec personne.

## 3. Alimenter le compte chaque mois

Le bot achète avec des **USDC** (un dollar numérique stable) présents dans ton portefeuille Spot.
Chaque mois, avant le 2 :

1. Fais ton virement de 50 CHF vers Binance.
2. Convertis-les en USDC (Binance → Convertir). Tu peux aussi automatiser cette étape
   avec un achat récurrent de USDC dans l'app Binance.

Si tu préfères une autre monnaie (par ex. EUR), change `"quote_asset"` dans `config.json`,
puis lance `python bot.py check` pour vérifier que les paires existent.

À noter : le budget et l'objectif sont exprimés dans cette monnaie (USDC), pas en CHF.

## 4. Tester en simulation

Ouvre un terminal dans le dossier et lance :

```
python bot.py check
python bot.py status
python bot.py run
```

Par défaut, `"dry_run": true` : le bot fait tout, mais n'envoie que des ordres de test
que Binance valide sans les exécuter. Regarde `logs\bot.log` et `journal.csv`.

## 5. Passer en réel

Quand tout te semble correct, dans `config.json` mets `"dry_run": false`.

## 6. Automatiser

Dans PowerShell, depuis le dossier :

```
powershell -ExecutionPolicy Bypass -File .\installer_tache.ps1
```

Le bot se lancera chaque jour à 10h. Il n'achète qu'une fois par mois (à partir du 2),
les autres jours il vérifie seulement la répartition et l'objectif.

---

## Réglages (`config.json`)

| Clé | Rôle |
|---|---|
| `dry_run` | `true` = simulation, `false` = réel |
| `monthly_budget` | Montant investi par mois |
| `buy_day` | Jour du mois à partir duquel l'achat se fait |
| `allocation` | Répartition cible (total = 1.0) |
| `rebalance_threshold` | Écart toléré avant rééquilibrage (0.10 = 10 points) |
| `rebalance_cooldown_days` | Délai minimum entre deux rééquilibrages |
| `min_portfolio_for_rebalance` | En dessous, pas de vente : les achats mensuels corrigent seuls |
| `goal_value` | Seuil d'alerte pour un éventuel retrait |
| `max_spend_per_run` | Garde-fou : plafond de dépense |

## Notifications sur ton téléphone (optionnel)

1. Sur Telegram, écris à **@BotFather** → `/newbot` → récupère le *token*.
2. Envoie un message à ton nouveau bot, puis ouvre
   `https://api.telegram.org/bot<TOKEN>/getUpdates` pour trouver ton `chat_id`.
3. Remplis `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID` dans `.env`.

Tu recevras un message à chaque achat, rééquilibrage, erreur et quand l'objectif est atteint.

## Fiscalité

`journal.csv` garde chaque transaction (date, montant, prix). Garde-le pour ta déclaration :
les cryptos se déclarent dans la fortune. Ce bot fait peu de transactions, ce qui va dans le
sens d'une gestion de fortune privée, mais vérifie avec ton canton en cas de doute.

## Tests

`python tests.py` lance les tests hors-ligne (faux Binance, aucun appel réseau).

---

*Ceci n'est pas un conseil financier. Les cryptos sont très volatiles : leur valeur peut baisser
fortement. N'investis que ce que tu peux te permettre de perdre.*
