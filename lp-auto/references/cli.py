#!/usr/bin/env python3
"""lp-auto CLI — unified entry point for the V3 LP auto-management skill.

Commands:
  init     — discover best pool, create instance dir, write config + initial state
  start    — run tick loop (foreground by default; use --install-cron to schedule)
  status   — print current instance state summary
  select   — run pool_selector (recommendation only, no action)
  switch   — execute cross-pool migration to selector's top candidate
  stop     — close the live position (strategy retained but no active LP)
  list     — list all lp-auto instances on this machine
  uninstall — remove instance (after confirming position is closed)

All commands honor --instance <name> to address a specific deployment (default "default").
State: ~/.lp-auto/instances/<name>/{state.json,config.json}
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
INSTANCES_ROOT = Path.home() / ".lp-auto" / "instances"
DEFAULT_INSTANCE = "default"


# ── Instance management ─────────────────────────────────────────────────────

def instance_dir(name: str) -> Path:
    return INSTANCES_ROOT / name


def ensure_instance_dir(name: str) -> Path:
    d = instance_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_instance_env(name: str) -> dict:
    env = dict(os.environ)
    env["LP_AUTO_INSTANCE_DIR"] = str(instance_dir(name))
    env["LP_AUTO_INSTANCE"] = name
    return env


def read_config(name: str) -> dict:
    p = instance_dir(name) / "config.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def write_config(name: str, cfg: dict):
    p = instance_dir(name) / "config.json"
    p.write_text(json.dumps(cfg, indent=2))


def read_state(name: str) -> dict:
    p = instance_dir(name) / "state.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def write_state(name: str, state: dict):
    p = instance_dir(name) / "state.json"
    p.write_text(json.dumps(state, indent=2))


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_init(args):
    sys.path.insert(0, str(SCRIPT_DIR))
    from token_registry import risk_tier, allowed
    from pool_config import fetch_pool_config
    from pool_compare import search_pools, score as classify_pool

    name = args.instance
    d = instance_dir(name)
    if d.exists() and (d / "config.json").exists():
        print(f"Instance '{name}' already exists at {d}")
        print("Use --force to re-initialize, or pick a different --instance name")
        if not args.force:
            return 1
    ensure_instance_dir(name)

    # Load defaults then overlay CLI args
    default_cfg = json.loads((SCRIPT_DIR / "config.default.json").read_text())
    cfg = {
        **default_cfg,
        "chain": args.chain,
        "max_risk": args.risk,
        "capital_usd": args.capital,
        "auto_switch": args.auto_switch,
    }

    # Discovery phase
    print(f"[init] Scanning {args.chain} DEX pools (max_risk={args.risk})...")
    raw = search_pools(args.chain, (args.tokens or "USDC,ETH,BTC,WBTC,cbBTC,cbETH,DAI,USDT").split(","))
    candidates = []
    for p in raw:
        c = classify_pool(p)
        if c["id"] and allowed(c["tier"], args.risk):
            candidates.append(c)
    if not candidates:
        print(f"[init] ✗ No pools found matching risk≤{args.risk} on {args.chain}")
        return 1
    print(f"[init] Found {len(candidates)} candidate pools within risk tier")

    # Pick target: user-specified, or top-APY (we'll refine with CE later if
    # user doesn't specify). For fast init we use name APY; `lp-auto select`
    # later does full CE re-ranking.
    if args.pool_id:
        selected = next((c for c in candidates if c["id"] == args.pool_id), None)
        if not selected:
            print(f"[init] ✗ Pool {args.pool_id} not among candidates (or wrong tier)")
            return 1
    else:
        candidates.sort(key=lambda c: -c["apy"])
        selected = candidates[0]
        print(f"[init] Auto-selected: {selected['pair']} id={selected['id']} "
              f"TVL=${selected['tvl']:,.0f} APY={selected['apy']*100:.1f}%")

    # Fetch full pool config from onchainos
    pool_cfg = fetch_pool_config(selected["id"], args.chain)
    if not pool_cfg:
        print(f"[init] ✗ Failed to fetch pool detail for {selected['id']}")
        return 1
    cfg["pool_config"] = {
        "investment_id": pool_cfg.investment_id,
        "chain": pool_cfg.chain,
        "chain_index": pool_cfg.chain_index,
        "token0_symbol": pool_cfg.token0_symbol,
        "token0_address": pool_cfg.token0_address,
        "token0_decimals": pool_cfg.token0_decimals,
        "token1_symbol": pool_cfg.token1_symbol,
        "token1_address": pool_cfg.token1_address,
        "token1_decimals": pool_cfg.token1_decimals,
        "fee_tier": pool_cfg.fee_tier,
        "tick_spacing": pool_cfg.tick_spacing,
    }

    write_config(name, cfg)

    # Seed state.json with blank position (so cl_lp.py on first tick detects
    # "no active position" and triggers initial deposit).
    state_seed = {
        "version": 1,
        "pool": {},
        "position": None,
        "stats": {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "initial_portfolio_usd": args.capital,
            "total_rebalances": 0,
            "total_fees_claimed_usd": 0.0,
        },
        "price_history": [],
        "rebalance_history": [],
        "errors": {},
    }
    write_state(name, state_seed)

    print(f"\n[init] ✓ Instance '{name}' initialized at {d}")
    print(f"       Pool: {pool_cfg.token0_symbol}/{pool_cfg.token1_symbol} "
          f"(id={pool_cfg.investment_id}, fee={pool_cfg.fee_tier*100:.2f}%)")
    print(f"       Capital: ${args.capital}  |  Gas reserve: {cfg['gas_reserve_eth']} ETH")
    print(f"       Auto-switch: {cfg['auto_switch']}")
    print()
    print(f"Next step:  lp-auto start --instance {name}")
    return 0


def cmd_start(args):
    """Run the tick loop. Foreground by default; --install-cron to schedule."""
    d = instance_dir(args.instance)
    if not (d / "config.json").exists():
        print(f"✗ Instance '{args.instance}' not initialized. Run: lp-auto init ...")
        return 1

    env = set_instance_env(args.instance)

    if args.install_cron:
        cron_line = (
            f"*/5 * * * * cd {SCRIPT_DIR} && LP_AUTO_INSTANCE_DIR={d} "
            f"python3 {SCRIPT_DIR}/cl_lp.py tick >> {d}/tick.log 2>&1"
        )
        # Append to user's crontab if not already there
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        if f"LP_AUTO_INSTANCE_DIR={d}" in existing:
            print(f"Cron entry already exists for instance '{args.instance}'")
        else:
            new_crontab = existing + "\n" + cron_line + "\n"
            subprocess.run(["crontab", "-"], input=new_crontab, text=True)
            print(f"✓ Installed cron entry (every 5 min):\n  {cron_line}")
        return 0

    # Foreground: just run tick once
    print(f"[start] Running tick for instance '{args.instance}'...")
    result = subprocess.run(
        ["python3", str(SCRIPT_DIR / "cl_lp.py"), "tick"],
        env=env,
    )
    return result.returncode


def cmd_status(args):
    state = read_state(args.instance)
    cfg = read_config(args.instance)
    if not cfg:
        print(f"Instance '{args.instance}' not found")
        return 1

    pc = cfg.get("pool_config") or {}
    pos = state.get("position") or {}
    stats = state.get("stats") or {}
    snap = state.get("_cached_snapshot") or {}

    print(f"== lp-auto instance: {args.instance} ==")
    print(f"Chain: {cfg.get('chain')}  |  Risk limit: {cfg.get('max_risk')}  |  Auto-switch: {cfg.get('auto_switch')}")
    if pc:
        print(f"Pool: {pc['token0_symbol']}/{pc['token1_symbol']} "
              f"fee={pc['fee_tier']*100:.2f}%  id={pc['investment_id']}")
    if pos and pos.get("token_id"):
        print(f"Position: token_id={pos['token_id']}  range=${pos.get('lower_price',0):.0f}-${pos.get('upper_price',0):.0f}")
        print(f"  entry=${pos.get('entry_price',0):.2f}  created={pos.get('created_at','?')}")
    else:
        print("Position: (none — initial deposit pending or closed)")

    if snap:
        print(f"Last tick: status={snap.get('status','?')}  price=${snap.get('price',0):.2f}")
        print(f"  portfolio=${snap.get('portfolio_usd',0):.2f}  "
              f"PnL={snap.get('pnl_usd',0):+.2f} ({snap.get('pnl_pct',0):+.1f}%)")
        print(f"  time_in_range={snap.get('time_in_range_pct',0):.0f}%  "
              f"rebalances={snap.get('total_rebalances',0)}")
    else:
        print("Last tick: never")

    sel_file = instance_dir(args.instance) / "pool_selector_state.json"
    if sel_file.exists():
        sel = json.loads(sel_file.read_text())
        runs = sel.get("runs", [])
        if runs:
            last = runs[-1]
            print(f"Selector last check: leader={last.get('leader','?')} "
                  f"uplift={last.get('uplift',0)*100:.1f}%  ({len(runs)} runs tracked)")
        rec = sel.get("recommend")
        if rec:
            print(f"🎯 Pending switch recommendation: → {rec.get('pair')} "
                  f"({rec['pool_id']})  uplift={rec['uplift']*100:.1f}%  "
                  f"streak={rec['streak']}")
            print(f"   Run `lp-auto switch` to execute.")
    return 0


def cmd_select(args):
    """Run pool_selector in recommendation mode."""
    d = instance_dir(args.instance)
    cfg = read_config(args.instance)
    pc = cfg.get("pool_config", {})
    env = set_instance_env(args.instance)
    cmd = [
        "python3", str(SCRIPT_DIR / "pool_selector.py"),
        "--capital", str(cfg.get("capital_usd", 500)),
        "--max-risk", cfg.get("max_risk", "medium"),
        "--chain", cfg.get("chain", "base"),
        "--threshold", str(cfg.get("switch_uplift_threshold", 0.3)),
        "--current-pool", pc.get("investment_id", ""),
    ]
    if args.lark:
        cmd += ["--lark", args.lark]
    return subprocess.run(cmd, env=env).returncode


def cmd_switch(args):
    """Execute cross-pool migration using selector's top recommendation."""
    selector_state_file = instance_dir(args.instance) / "pool_selector_state.json"
    if not selector_state_file.exists():
        print("✗ No selector run yet. Run `lp-auto select` first.")
        return 1
    sel = json.loads(selector_state_file.read_text())
    recommend = sel.get("recommend")
    if not recommend:
        print("✗ No pending switch recommendation — either streak not yet met,")
        print("  or current pool is still optimal. Run `lp-auto select`.")
        return 1

    target_id = str(recommend["pool_id"])
    tick_lo = recommend["tick_lo"]
    tick_hi = recommend["tick_hi"]
    env = set_instance_env(args.instance)
    print(f"Switching to pool {target_id} ({recommend.get('pair')}) "
          f"tick [{tick_lo}, {tick_hi}]  uplift {recommend.get('uplift',0)*100:.1f}%")
    cmd = [
        "python3", str(SCRIPT_DIR / "pool_switch.py"),
        target_id, str(tick_lo), str(tick_hi),
    ]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.lark:
        cmd += ["--lark", args.lark]
    rc = subprocess.run(cmd, env=env).returncode
    if rc == 0 and not args.dry_run:
        # After successful switch, update config's pool_config to match new pool
        cfg = read_config(args.instance)
        sys.path.insert(0, str(SCRIPT_DIR))
        from pool_config import fetch_pool_config
        new_pc = fetch_pool_config(target_id, cfg.get("chain", "base"))
        if new_pc:
            cfg["pool_config"] = {
                "investment_id": new_pc.investment_id,
                "chain": new_pc.chain,
                "chain_index": new_pc.chain_index,
                "token0_symbol": new_pc.token0_symbol,
                "token0_address": new_pc.token0_address,
                "token0_decimals": new_pc.token0_decimals,
                "token1_symbol": new_pc.token1_symbol,
                "token1_address": new_pc.token1_address,
                "token1_decimals": new_pc.token1_decimals,
                "fee_tier": new_pc.fee_tier,
                "tick_spacing": new_pc.tick_spacing,
            }
            write_config(args.instance, cfg)
            print(f"✓ Updated config.pool_config to reflect new pool")
        # Clear the recommendation so it won't re-trigger
        sel.pop("recommend", None)
        selector_state_file.write_text(json.dumps(sel, indent=2))
    return rc


def cmd_stop(args):
    """Close active position. Does not remove the instance directory."""
    state = read_state(args.instance)
    pos = state.get("position") or {}
    if not pos.get("token_id"):
        print("No active position to close")
        return 0
    env = set_instance_env(args.instance)
    result = subprocess.run(
        ["python3", str(SCRIPT_DIR / "cl_lp.py"), "close"],
        env=env,
    )
    return result.returncode


def cmd_list(args):
    if not INSTANCES_ROOT.exists():
        print("No lp-auto instances yet. Run: lp-auto init ...")
        return 0
    for d in sorted(INSTANCES_ROOT.iterdir()):
        if not d.is_dir():
            continue
        cfg = json.loads((d / "config.json").read_text()) if (d / "config.json").exists() else {}
        state = json.loads((d / "state.json").read_text()) if (d / "state.json").exists() else {}
        pc = cfg.get("pool_config") or {}
        pos = state.get("position") or {}
        print(f"  {d.name:<20} {cfg.get('chain','?'):<10} "
              f"{pc.get('token0_symbol','?')}/{pc.get('token1_symbol','?'):<8} "
              f"{'active' if pos.get('token_id') else 'idle':<8} "
              f"max_risk={cfg.get('max_risk','?')}")
    return 0


def cmd_uninstall(args):
    d = instance_dir(args.instance)
    if not d.exists():
        print(f"Instance '{args.instance}' does not exist")
        return 1
    state = read_state(args.instance)
    pos = state.get("position") or {}
    if pos.get("token_id") and not args.force:
        print(f"✗ Active position {pos['token_id']} detected. Run `lp-auto stop` first, "
              f"or pass --force to uninstall anyway (funds stay on chain).")
        return 1
    shutil.rmtree(d)
    print(f"✓ Removed instance '{args.instance}' (funds on-chain are unaffected)")
    return 0


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog="lp-auto", description=__doc__)
    parser.add_argument("--instance", default=DEFAULT_INSTANCE,
                        help="Instance name (default: 'default')")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--chain", default="base")
    p_init.add_argument("--risk", default="medium")
    p_init.add_argument("--capital", type=float, default=500)
    p_init.add_argument("--pool-id", help="Specific investmentId (skips auto-selection)")
    p_init.add_argument("--tokens", help="Comma-separated discovery seeds")
    p_init.add_argument("--auto-switch", action="store_true")
    p_init.add_argument("--force", action="store_true", help="Re-init existing instance")
    p_init.set_defaults(func=cmd_init)

    p_start = sub.add_parser("start")
    p_start.add_argument("--install-cron", action="store_true")
    p_start.set_defaults(func=cmd_start)

    p_status = sub.add_parser("status")
    p_status.set_defaults(func=cmd_status)

    p_select = sub.add_parser("select")
    p_select.add_argument("--lark", default="")
    p_select.set_defaults(func=cmd_select)

    p_switch = sub.add_parser("switch")
    p_switch.add_argument("--dry-run", action="store_true")
    p_switch.add_argument("--lark", default="")
    p_switch.set_defaults(func=cmd_switch)

    p_stop = sub.add_parser("stop")
    p_stop.set_defaults(func=cmd_stop)

    p_list = sub.add_parser("list")
    p_list.set_defaults(func=cmd_list)

    p_uninstall = sub.add_parser("uninstall")
    p_uninstall.add_argument("--force", action="store_true")
    p_uninstall.set_defaults(func=cmd_uninstall)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
