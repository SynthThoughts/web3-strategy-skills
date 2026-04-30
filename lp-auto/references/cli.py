#!/usr/bin/env python3
"""lp-auto CLI — unified entry point for the V3 LP auto-management skill.

Commands:
  init                — discover best pool, create instance dir, write config + initial state
  start               — run tick once in foreground (one-shot; for manual/cron use)
  daemon              — portable while-loop: tick + sleep; SIGTERM 优雅退出 (cross-platform)
  install             — one-shot: detect platform → install scheduler → register → verify
  scheduler-register  — record external scheduler (systemd/launchd/cron/...) in scheduler.json
  status              — print instance state + scheduler health self-check
  select              — run pool_selector (recommendation only, no action)
  switch              — execute cross-pool migration to selector's top candidate
  stop                — close the live position (strategy retained but no active LP)
  list                — list all lp-auto instances on this machine
  uninstall           — remove instance (after confirming position is closed)

All commands honor --instance <name> to address a specific deployment (default "default").
State: ~/.lp-auto/instances/<name>/{state.json,config.json,scheduler.json}

Scheduling model (cross-platform):
  Tier 1 (portable):   `lp-auto daemon` — works on Linux / macOS / Windows
  Tier 2 (platform):   AI-installed systemd unit / launchd plist / Task Scheduler;
                        AI must call `scheduler-register` afterwards so status
                        can self-check.
  Tier 3 (fallback):   crontab (Linux/Mac only)
See SKILL.md "Scheduling" for the full decision tree + references/scheduler/ for templates.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import shutil
import signal
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


def _load_instance_dotenv(name: str) -> dict[str, str]:
    """Parse `<instance>/.env` if present. Supports `KEY=VALUE` and
    `export KEY=VALUE`, strips surrounding quotes, skips blanks/comments.
    Silent if the file is missing — `.env` is optional.
    """
    env_file = instance_dir(name) / ".env"
    loaded: dict[str, str] = {}
    if not env_file.exists():
        return loaded
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # Strip matching single/double quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        loaded[key] = val
    return loaded


def set_instance_env(name: str) -> dict:
    """Build an env dict for subprocesses. Includes this process's env +
    `<instance>/.env` (if present) + instance location markers. `.env` values
    override parent env so per-instance secrets win over stale globals."""
    env = dict(os.environ)
    env.update(_load_instance_dotenv(name))
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


# ── Scheduler tracking ──────────────────────────────────────────────────────

VALID_SCHEDULER_TYPES = {
    "systemd-user",       # Linux user unit
    "systemd-system",     # Linux system unit (rare; usually prefer user)
    "launchd",            # macOS
    "windows-task",       # Windows Task Scheduler
    "cron",               # *nix crontab
    "daemon-foreground",  # `lp-auto daemon` in tmux/screen/nohup
    "manual",             # user runs `lp-auto start` themselves
}


def read_scheduler(name: str) -> dict:
    p = instance_dir(name) / "scheduler.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def write_scheduler(name: str, info: dict):
    p = instance_dir(name) / "scheduler.json"
    p.write_text(json.dumps(info, indent=2))


def _now_iso() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_scheduler_alive(info: dict) -> tuple[bool, str]:
    """Return (alive, detail) for the registered scheduler.

    Dispatches per `type`. Never raises — returns (False, reason) on error.
    """
    t = info.get("type", "")
    ident = info.get("id", "")
    if not t:
        return (False, "no scheduler registered")

    try:
        if t == "systemd-user":
            r = subprocess.run(
                ["systemctl", "--user", "is-active", ident],
                capture_output=True, text=True, timeout=5,
            )
            return (r.stdout.strip() == "active", r.stdout.strip() or r.stderr.strip() or "unknown")
        if t == "systemd-system":
            r = subprocess.run(
                ["systemctl", "is-active", ident],
                capture_output=True, text=True, timeout=5,
            )
            return (r.stdout.strip() == "active", r.stdout.strip() or r.stderr.strip() or "unknown")
        if t == "launchd":
            r = subprocess.run(
                ["launchctl", "list", ident],
                capture_output=True, text=True, timeout=5,
            )
            return (r.returncode == 0, "loaded" if r.returncode == 0 else "not loaded")
        if t == "windows-task":
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", ident, "/FO", "LIST"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return (False, "task not found")
            for line in r.stdout.splitlines():
                if line.lower().startswith("status:"):
                    return (True, line.split(":", 1)[1].strip())
            return (True, "registered")
        if t == "cron":
            r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
            marker = ident or f"LP_AUTO_INSTANCE_DIR={instance_dir(info.get('instance',''))}"
            return (marker in r.stdout, "entry found" if marker in r.stdout else "entry missing")
        if t == "daemon-foreground":
            pid_path = info.get("pid_file")
            if pid_path and Path(pid_path).exists():
                pid = int(Path(pid_path).read_text().strip())
                try:
                    os.kill(pid, 0)
                    return (True, f"pid {pid} alive")
                except (ProcessLookupError, PermissionError):
                    return (False, f"pid {pid} dead")
            return (False, "no pid file")
        if t == "manual":
            return (True, "manual operation (no auto-run)")
    except FileNotFoundError as e:
        return (False, f"command missing: {e.filename}")
    except Exception as e:  # pragma: no cover - defensive
        return (False, f"check failed: {e}")
    return (False, f"unknown scheduler type: {t}")


def _last_tick_age_seconds(instance_name: str, state: dict) -> Optional[int]:
    """Seconds since last tick wrote state. Tries (in order):
      1. `_cached_snapshot.updated_at` / `.timestamp` if cl_lp.py records one
      2. state.json mtime (works for any cl_lp.py version — cheapest + most robust)
    """
    snap = state.get("_cached_snapshot") or {}
    ts = snap.get("updated_at") or snap.get("timestamp")
    if ts:
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = _dt.datetime.strptime(ts, fmt)
                return int((_dt.datetime.utcnow() - dt).total_seconds())
            except ValueError:
                continue
    # Fallback: state.json mtime (updated every tick by cl_lp.save_state)
    p = instance_dir(instance_name) / "state.json"
    if p.exists():
        return int(time.time() - p.stat().st_mtime)
    return None


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


def cmd_report(args):
    """Invoke cl_lp.py's `report` subcommand (daily summary).

    This is a thin pass-through so schedulers can run reports via the same
    `lp-auto` entrypoint used for ticks — keeps instance/env plumbing
    consistent and avoids depending on the cl_lp.py path directly.
    """
    d = instance_dir(args.instance)
    if not (d / "config.json").exists():
        print(f"✗ Instance '{args.instance}' not initialized. Run: lp-auto init ...")
        return 1
    env = set_instance_env(args.instance)
    return subprocess.run(
        ["python3", str(SCRIPT_DIR / "cl_lp.py"), "report"],
        env=env,
    ).returncode


def cmd_start(args):
    """Run a single tick in the foreground (one-shot).

    For recurring execution, pick one:
      - `lp-auto daemon`                     — portable while-loop (all OS)
      - platform-native scheduler + `lp-auto scheduler-register`
        (systemd --user / launchd / Task Scheduler / cron)

    See SKILL.md "Scheduling" section for the full playbook.
    """
    d = instance_dir(args.instance)
    if not (d / "config.json").exists():
        print(f"✗ Instance '{args.instance}' not initialized. Run: lp-auto init ...")
        return 1

    if args.install_cron:
        print("⚠ --install-cron is deprecated. Use one of:")
        print(f"    lp-auto daemon --instance {args.instance}                 # Tier 1 portable")
        print("    (AI installs systemd/launchd/schtasks, then calls scheduler-register)")
        print("  Cron fallback is still supported; see references/scheduler/cron.example.")
        return 2

    env = set_instance_env(args.instance)
    print(f"[start] Running single tick for instance '{args.instance}'...")
    result = subprocess.run(
        ["python3", str(SCRIPT_DIR / "cl_lp.py"), "tick"],
        env=env,
    )
    return result.returncode


def cmd_daemon(args):
    """Portable while-loop: tick → sleep → tick. SIGTERM / SIGINT → graceful exit.

    This is the cross-platform path. The user / AI / OS service manager is
    responsible for *keeping this process alive* (systemd, launchd, Task
    Scheduler, nohup+tmux, etc.). This function does not daemonize itself.
    """
    d = instance_dir(args.instance)
    if not (d / "config.json").exists():
        print(f"✗ Instance '{args.instance}' not initialized. Run: lp-auto init ...")
        return 1

    env = set_instance_env(args.instance)
    interval = max(30, int(args.interval))  # safety floor
    pid_file = d / "daemon.pid"
    pid_file.write_text(str(os.getpid()))

    stop = {"flag": False}

    def _graceful(signum, _frame):
        print(f"[daemon] received signal {signum}; finishing current tick then exit")
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    print(f"[daemon] instance='{args.instance}' interval={interval}s pid={os.getpid()}")
    try:
        while not stop["flag"]:
            t0 = time.monotonic()
            try:
                subprocess.run(
                    ["python3", str(SCRIPT_DIR / "cl_lp.py"), "tick"],
                    env=env,
                    check=False,
                )
            except Exception as e:  # never let a tick crash kill the loop
                print(f"[daemon] tick raised {e!r}; continuing")
            if stop["flag"]:
                break
            elapsed = time.monotonic() - t0
            sleep_for = max(1.0, interval - elapsed)
            # sleep in small chunks so SIGTERM is responsive
            while sleep_for > 0 and not stop["flag"]:
                chunk = min(2.0, sleep_for)
                time.sleep(chunk)
                sleep_for -= chunk
    finally:
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
    print("[daemon] exited cleanly")
    return 0


def _detect_default_scheduler() -> str:
    """Pick the best-fit scheduler type for this platform.

    Defaults aim for low-friction, no-sudo options. User can override with
    `lp-auto install --scheduler <type>`.
    """
    system = platform.system()
    if system == "Darwin":
        return "launchd"
    if system == "Windows":
        return "windows-task"
    # Linux/other — crontab is universally available and needs no user
    # session tricks (unlike systemd --user, which needs loginctl linger on
    # headless servers).
    if shutil.which("crontab"):
        return "cron"
    if shutil.which("systemctl"):
        return "systemd-user"
    return "daemon-foreground"


def _render_template(template_path: Path, subs: dict[str, str]) -> str:
    text = template_path.read_text()
    for k, v in subs.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def _install_cron(subs: dict[str, str], instance: str) -> tuple[bool, str]:
    """Append a cron line via `crontab -l | crontab -`. Idempotent on marker."""
    template = SCRIPT_DIR / "scheduler" / "cron.example"
    rendered = _render_template(template, subs)
    # Extract just the schedule line (skip comment block)
    cron_line = next((l for l in rendered.splitlines()
                      if l.strip() and not l.lstrip().startswith("#")), "")
    if not cron_line:
        return (False, "template produced no cron line")
    marker = f"LP_AUTO_INSTANCE_DIR={subs['HOME']}/.lp-auto/instances/{instance}"
    existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout or ""
    if marker in existing:
        return (True, "already installed (marker found)")
    new_tab = existing.rstrip() + "\n\n# lp-auto instance={} (installed by `lp-auto install`)\n{}\n".format(
        instance, cron_line,
    )
    r = subprocess.run(["crontab", "-"], input=new_tab, text=True, capture_output=True)
    if r.returncode != 0:
        return (False, f"crontab write failed: {r.stderr.strip()}")
    return (True, marker)


def _install_systemd_user(subs: dict[str, str], instance: str) -> tuple[bool, str]:
    template = SCRIPT_DIR / "scheduler" / "systemd-user.service.example"
    rendered = _render_template(template, subs)
    unit_dir = Path(subs["HOME"]) / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_name = f"lp-auto@{instance}.service"
    (unit_dir / unit_name).write_text(rendered)
    for cmd in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", unit_name],
    ):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return (False, f"{' '.join(cmd)} failed: {r.stderr.strip()}")
    return (True, unit_name)


def _install_launchd(subs: dict[str, str], instance: str) -> tuple[bool, str]:
    template = SCRIPT_DIR / "scheduler" / "launchd.plist.example"
    rendered = _render_template(template, subs)
    plist_dir = Path(subs["HOME"]) / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    label = f"ai.lp-auto.{instance}"
    plist_path = plist_dir / f"{label}.plist"
    plist_path.write_text(rendered)
    # Unload first to allow re-install; ignore errors.
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    r = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
    if r.returncode != 0:
        return (False, f"launchctl load failed: {r.stderr.strip()}")
    return (True, label)


def _install_windows_task(subs: dict[str, str], instance: str) -> tuple[bool, str]:
    template = SCRIPT_DIR / "scheduler" / "windows-task.xml.example"
    rendered = _render_template(template, subs)
    task_name = f"lp-auto {instance}"
    xml_tmp = Path(subs["HOME"]) / f".lp-auto-task-{instance}.xml"
    xml_tmp.write_text(rendered, encoding="utf-16")
    r = subprocess.run(
        ["schtasks", "/Create", "/XML", str(xml_tmp), "/TN", task_name, "/F"],
        capture_output=True, text=True,
    )
    xml_tmp.unlink(missing_ok=True)
    if r.returncode != 0:
        return (False, f"schtasks /Create failed: {r.stderr.strip()}")
    subprocess.run(["schtasks", "/Run", "/TN", task_name], capture_output=True)
    return (True, task_name)


def _install_daemon_foreground(subs: dict[str, str], instance: str) -> tuple[bool, str]:
    """Spawn `lp-auto daemon` under nohup. User is responsible for re-spawn
    after reboot — this is the lowest-friction fallback, not truly persistent."""
    d = instance_dir(instance)
    log_path = d / "daemon.log"
    pid_path = d / "daemon.pid"
    with open(log_path, "a") as lf:
        proc = subprocess.Popen(
            [subs["PYTHON"], subs["CLI"],
             "--instance", instance, "daemon", "--interval", subs["INTERVAL"]],
            stdout=lf, stderr=subprocess.STDOUT, start_new_session=True,
        )
    # Give the daemon a moment to write its own pid file
    for _ in range(20):
        time.sleep(0.1)
        if pid_path.exists():
            break
    return (True, str(pid_path))


def cmd_install(args):
    """One-shot: detect platform → install scheduler → register → verify.

    Defaults:
      Linux   → cron
      macOS   → launchd
      Windows → windows-task
    Override with --scheduler {cron, systemd-user, launchd, windows-task,
                               daemon-foreground, manual}.
    """
    d = instance_dir(args.instance)
    if not (d / "config.json").exists():
        print(f"✗ Instance '{args.instance}' not initialized. Run: lp-auto init ...")
        return 1

    sched_type = args.scheduler or _detect_default_scheduler()
    if sched_type not in VALID_SCHEDULER_TYPES:
        print(f"✗ --scheduler must be one of: {sorted(VALID_SCHEDULER_TYPES)}")
        return 1

    python_bin = sys.executable
    subs = {
        "PYTHON": python_bin,
        "CLI": str(SCRIPT_DIR / "cli.py"),
        "INSTANCE": args.instance,
        "HOME": str(Path.home()),
        "USER": os.environ.get("USER") or os.environ.get("USERNAME") or "",
        "INTERVAL": str(args.interval),
    }

    print(f"[install] platform={platform.system()} scheduler={sched_type} "
          f"interval={args.interval}s")

    installers = {
        "cron": _install_cron,
        "systemd-user": _install_systemd_user,
        "launchd": _install_launchd,
        "windows-task": _install_windows_task,
        "daemon-foreground": _install_daemon_foreground,
    }
    install_id = ""
    if sched_type == "manual":
        print("[install] type=manual — skipping installer; you will run ticks yourself.")
    else:
        fn = installers.get(sched_type)
        if not fn:
            print(f"✗ installer for {sched_type} not implemented")
            return 1
        ok, detail = fn(subs, args.instance)
        if not ok:
            print(f"✗ Install failed: {detail}")
            return 1
        install_id = detail
        print(f"[install] ✓ {sched_type} installed (id={install_id})")

    # Register so status can self-check
    info = {
        "instance": args.instance,
        "type": sched_type,
        "id": install_id,
        "installed_at": _now_iso(),
        "platform": platform.system(),
        "platform_release": platform.release(),
        "interval_seconds": args.interval,
    }
    if sched_type == "daemon-foreground":
        info["pid_file"] = str(d / "daemon.pid")
    write_scheduler(args.instance, info)
    print(f"[install] ✓ scheduler.json written")

    # Self-check
    alive, detail = _check_scheduler_alive(info)
    marker = "✓" if alive else "⚠"
    print(f"[install] {marker} Self-check: {detail}")
    print()
    print(f"Next: watch a tick land (every {args.interval}s):")
    print(f"  tail -f {d}/cl_lp.log   # or daemon.log / cron.log")
    print(f"  lp-auto --instance {args.instance} status")
    return 0 if alive else 2


def cmd_scheduler_register(args):
    """Record how this instance is being scheduled so `status` can self-check.

    Called by the AI *after* it installs a platform-native scheduler
    (systemd-user / launchd / Task Scheduler / cron). For `daemon-foreground`
    the `lp-auto daemon` command writes `daemon.pid` itself; pointing this at
    that file lets `status` verify the process is alive.

    Example:
      lp-auto scheduler-register --type systemd-user --id lp-auto@prod.service
      lp-auto scheduler-register --type launchd       --id ai.lp-auto.prod
      lp-auto scheduler-register --type windows-task  --id "lp-auto prod"
      lp-auto scheduler-register --type cron          --id "LP_AUTO_INSTANCE_DIR=/home/u/.lp-auto/instances/prod"
      lp-auto scheduler-register --type daemon-foreground --pid-file ~/.lp-auto/instances/prod/daemon.pid
      lp-auto scheduler-register --type manual
    """
    if args.type not in VALID_SCHEDULER_TYPES:
        print(f"✗ --type must be one of: {sorted(VALID_SCHEDULER_TYPES)}")
        return 1
    d = instance_dir(args.instance)
    if not d.exists():
        print(f"✗ Instance '{args.instance}' not initialized. Run: lp-auto init ...")
        return 1
    info = {
        "instance": args.instance,
        "type": args.type,
        "id": args.id or "",
        "installed_at": _now_iso(),
        "platform": platform.system(),
        "platform_release": platform.release(),
    }
    if args.pid_file:
        info["pid_file"] = str(Path(args.pid_file).expanduser())
    if args.command:
        info["command"] = args.command
    if args.interval:
        info["interval_seconds"] = int(args.interval)
    write_scheduler(args.instance, info)
    alive, detail = _check_scheduler_alive(info)
    marker = "✓" if alive else "⚠"
    print(f"{marker} Registered scheduler type={args.type} id={info['id'] or '-'} ({detail})")
    print(f"  scheduler.json → {d / 'scheduler.json'}")
    if not alive:
        print("  Note: scheduler not active right now. Re-run `lp-auto status` after you start it.")
    return 0


def cmd_status(args):
    state = read_state(args.instance)
    cfg = read_config(args.instance)
    if not cfg:
        print(f"✗ Instance '{args.instance}' not found.")
        print("  Run `lp-auto init ...` to create it, or `lp-auto list` to see existing instances.")
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

    # ── Scheduler self-check ──
    sched = read_scheduler(args.instance)
    print()
    print("-- Scheduler --")
    if not sched:
        print("⚠ No scheduler registered. Pick one and call scheduler-register:")
        print(f"    lp-auto daemon --instance {args.instance}    # Tier 1 portable")
        print("    (or AI installs systemd/launchd/schtasks, then `scheduler-register`)")
    else:
        alive, detail = _check_scheduler_alive(sched)
        marker = "✓" if alive else "✗"
        print(f"{marker} type={sched.get('type','?')} id={sched.get('id') or '-'} ({detail})")
        if sched.get("installed_at"):
            print(f"  installed: {sched['installed_at']}  host={sched.get('platform','?')}")
        # Last-tick freshness check
        age = _last_tick_age_seconds(args.instance, state)
        expected = int(sched.get("interval_seconds") or 300)
        if age is None:
            print(f"  last tick: never (expect a tick within ~{expected}s)")
        else:
            tol = max(expected * 2, 600)
            flag = "✓" if age <= tol else "⚠"
            print(f"  last tick age: {age}s  (tolerance {tol}s)  {flag}")
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

    p_report = sub.add_parser("report",
                              help="Run the daily report (pass-through to cl_lp.py report)")
    p_report.set_defaults(func=cmd_report)

    p_start = sub.add_parser("start", help="Run a single tick (one-shot)")
    p_start.add_argument("--install-cron", action="store_true",
                         help="(deprecated) use `daemon` or install a platform-native scheduler")
    p_start.set_defaults(func=cmd_start)

    p_daemon = sub.add_parser("daemon", help="Portable while-loop (Linux/Mac/Windows)")
    p_daemon.add_argument("--interval", type=int, default=300,
                          help="Seconds between ticks (min 30, default 300)")
    p_daemon.set_defaults(func=cmd_daemon)

    p_install = sub.add_parser("install",
                               help="One-shot: auto-detect platform and install a scheduler")
    p_install.add_argument("--scheduler", default="",
                           help="Override detection: cron / systemd-user / launchd / "
                                "windows-task / daemon-foreground / manual")
    p_install.add_argument("--interval", type=int, default=300,
                           help="Tick interval in seconds (default 300)")
    p_install.set_defaults(func=cmd_install)

    p_reg = sub.add_parser("scheduler-register",
                           help="Record external scheduler so `status` can self-check")
    p_reg.add_argument("--type", required=True,
                       help=f"One of: {sorted(VALID_SCHEDULER_TYPES)}")
    p_reg.add_argument("--id", default="",
                       help="Scheduler-specific identifier (unit name / task name / cron marker)")
    p_reg.add_argument("--pid-file", default="",
                       help="For daemon-foreground: path to the pid file (default: <instance>/daemon.pid)")
    p_reg.add_argument("--command", default="",
                       help="Optional: the exact command the scheduler runs (for audit)")
    p_reg.add_argument("--interval", type=int, default=0,
                       help="Optional: tick interval in seconds (used by status freshness check)")
    p_reg.set_defaults(func=cmd_scheduler_register)

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
