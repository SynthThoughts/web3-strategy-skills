# lp-auto — Quickstart (cold start → live LP position)

Go from a fresh clone to a live, auto-managed Uniswap V3 LP position. After the
one-time setup, **one command** (`lp-auto deploy`) selects a pool, opens the
position on-chain, and schedules ongoing management.

> All on-chain actions (deposit / redeem / balances / signing) run through the
> **OKX OnchainOS CLI** (`onchainos`). The private key lives inside onchainos —
> this repo never sees it. onchainos is a separate binary you install once.

---

## Requirements

- **Python 3.10+** — no `pip install` needed (the engine is pure standard library).
- **Linux or macOS** (uses `fcntl` file locking; Windows daemon mode is limited).
- **OKX OnchainOS CLI** installed and logged in (Step 0).
- A wallet **funded on your target chain** (default Base): native **ETH for gas**
  (≥ `gas_reserve_eth`, default 0.01) **plus your capital**.

---

## Step 0 — Install & log in to onchainos, then fund the wallet

1. Install the OKX OnchainOS CLI and make sure it is on your `PATH`
   (follow the OKX OnchainOS install docs for your OS/arch). Verify:
   ```bash
   onchainos --version
   ```
2. Log in / create your wallet, then confirm it is ready:
   ```bash
   onchainos wallet status      # expect loggedIn: true
   onchainos wallet addresses   # note your EVM address
   ```
3. Fund that address **on the chain you'll use** (default `base`): send native
   **ETH** (covers gas + provides capital). The deposit step balances ETH/USDC
   for you, so ETH-only funding is fine.

## Step 1 — Install the CLI (one time)

```bash
bash references/install.sh        # symlinks `lp-auto` into ~/.local/bin
# If ~/.local/bin isn't on PATH, the script tells you the line to add.
```

## Step 2 — Deploy (the one command)

```bash
lp-auto deploy --chain base --risk medium --capital 500
```

This runs the full cold-start pipeline and **stops at the first failed
precondition without touching the chain**:

1. **Preflight** — onchainos present, wallet logged in, funds on `--chain`.
2. **init** — scan pools in the risk tier, pick the best, write config + state.
3. **first tick** — open the initial LP position on-chain (the actual mint).
4. **scheduler** — install a platform scheduler (cron / systemd / launchd /
   Task Scheduler) so the position is auto-managed every ~5 min.
5. **status** — print the live position, value, range, and scheduler health.

Check it any time:
```bash
lp-auto status          # position, PnL, range, scheduler health
lp-auto doctor          # re-run preflight only (read-only)
```

---

## Common options

```bash
# Conservative stablecoin/LST tier, $300, on Arbitrum:
lp-auto deploy --chain arbitrum --risk low --capital 300

# Pin a specific pool (skip auto-selection):
lp-auto deploy --chain base --pool-id <investmentId> --capital 500

# Run a second instance side by side:
lp-auto --instance growth deploy --chain base --risk medium-high --capital 1000
```

Risk tiers: `very-low low medium medium-high high very-high`
(see `references/token-risk-classification.md`). Full parameter list and the
scheduling model are in `SKILL.md`.

---

## Stopping / removing

```bash
lp-auto stop                 # close the position (funds back to wallet); scheduler stays
lp-auto uninstall            # remove the instance (refuses if a position is still open)
```
Removing the scheduler is per-platform — see `SKILL.md` → "停止调度".

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `onchainos CLI not found on PATH` | Install onchainos (Step 0) and add it to PATH. |
| `No logged-in wallet` | `onchainos wallet status`; log in if `loggedIn: false`. |
| `⚠ Below requested capital` | Fund the wallet on your chain (native ETH for gas + capital). |
| `Balance too low for initial deposit` | Same — add funds, then re-run `lp-auto deploy`. |
| `lp-auto: command not found` | Re-run `bash references/install.sh` and ensure `~/.local/bin` is on PATH. |

`lp-auto deploy` is safe to re-run: if the instance is already initialized it
reuses it (pass `--force` to re-select the pool), and an existing position is
managed rather than duplicated.
