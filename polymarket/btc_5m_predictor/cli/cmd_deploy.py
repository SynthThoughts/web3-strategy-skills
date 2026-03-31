"""Deployment commands: promote, shadow, compare."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path


def run(args: argparse.Namespace) -> int:
    if args.deploy_action is None:
        print("Usage: btc deploy {promote|shadow|compare}")
        return 1

    if args.deploy_action == "promote":
        return _promote(args.run_id)
    elif args.deploy_action == "shadow":
        return _shadow(args)
    elif args.deploy_action == "compare":
        return _compare()

    return 1


def _promote(run_id: str) -> int:
    """Promote a model to production."""
    import db

    # Look up run
    con = db.get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT version FROM model_runs WHERE run_id = ?", [run_id]
        ).fetchone()
    finally:
        con.close()

    if row is None:
        print(f"Error: run_id '{run_id}' not found")
        return 1

    version = row[0]
    model_dir = Path(f"models/{version}")
    if not model_dir.exists():
        print(f"Error: model directory '{model_dir}' not found")
        return 1

    print(f"  Promoting {version} (run_id: {run_id})")

    # Atomic update of config.py
    try:
        _update_config_version("ACTIVE_MODEL_VERSION", version)
        print(f"  Local config updated: ACTIVE_MODEL_VERSION = '{version}'")
    except Exception as e:
        print(f"  Error updating local config: {e}")
        return 1

    # Deploy to VPS
    deploy_script = Path("deploy/deploy.sh")
    if deploy_script.exists():
        print("  Syncing model to VPS...")
        result = subprocess.run(
            ["bash", str(deploy_script), "sync-model"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            print("  VPS sync complete.")
        else:
            print(f"  VPS sync failed (exit {result.returncode})")
            if result.stderr:
                print(f"  stderr: {result.stderr[:200]}")
            print("  Local config was updated. Retry with: bash deploy/deploy.sh sync-model")
            return 1
    else:
        print(f"  Warning: {deploy_script} not found. Skipping VPS sync.")

    print(f"\n  Promotion complete: {version}")
    return 0


def _shadow(args: argparse.Namespace) -> int:
    """Manage shadow/challenger models."""
    action = getattr(args, "shadow_action", None)
    if action is None:
        print("Usage: btc deploy shadow {add|remove|list}")
        return 1

    if action == "add":
        return _shadow_add(args.run_id)
    elif action == "remove":
        return _shadow_remove(args.version)
    elif action == "list":
        return _shadow_list()

    return 1


def _shadow_add(run_id: str) -> int:
    """Add a model as a challenger."""
    import db

    con = db.get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT version FROM model_runs WHERE run_id = ?", [run_id]
        ).fetchone()
    finally:
        con.close()

    if row is None:
        print(f"Error: run_id '{run_id}' not found")
        return 1

    version = row[0]

    # Read current challengers
    challengers = _read_challenger_list()
    if version in challengers:
        print(f"  {version} is already a challenger.")
        return 0

    challengers.append(version)
    _update_config_challenger_list(challengers)
    print(f"  Added {version} as challenger. Active challengers: {challengers}")
    return 0


def _shadow_remove(version: str) -> int:
    """Remove a challenger model."""
    challengers = _read_challenger_list()
    if version not in challengers:
        print(f"  {version} is not in the challenger list.")
        return 1

    challengers.remove(version)
    _update_config_challenger_list(challengers)
    print(f"  Removed {version}. Remaining challengers: {challengers}")
    return 0


def _shadow_list() -> int:
    """List current challenger models."""
    challengers = _read_challenger_list()
    if not challengers:
        print("  No challenger models configured.")
    else:
        print(f"\n  === Challenger Models ===\n")
        for v in challengers:
            print(f"    - {v}")
    return 0


def _compare() -> int:
    """Compare champion vs challenger performance."""
    import db

    con = db.get_connection(read_only=True)
    try:
        # Check if live_predictions table has challenger data
        try:
            champ = con.execute(
                "SELECT count(*), avg(CASE WHEN correct THEN 1.0 ELSE 0.0 END) "
                "FROM live_predictions WHERE is_challenger = false"
            ).fetchone()
            challenger = con.execute(
                "SELECT count(*), avg(CASE WHEN correct THEN 1.0 ELSE 0.0 END) "
                "FROM live_predictions WHERE is_challenger = true"
            ).fetchone()
        except Exception:
            print("  No live_predictions table or missing is_challenger column.")
            print("  Recommendation: INSUFFICIENT_DATA")
            return 0
    finally:
        con.close()

    champ_n, champ_acc = (champ[0] or 0), (champ[1] or 0)
    chal_n, chal_acc = (challenger[0] or 0), (challenger[1] or 0)

    print(f"\n=== Champion vs Challenger Comparison ===\n")
    print(f"  {'':>20s}  {'Champion':>12s}  {'Challenger':>12s}")
    print(f"  {'─' * 20}  {'─' * 12}  {'─' * 12}")
    print(f"  {'Samples':<20s}  {champ_n:>12,d}  {chal_n:>12,d}")
    print(f"  {'Accuracy':<20s}  {champ_acc:>12.2%}  {chal_acc:>12.2%}")

    # Recommendation
    min_samples = 200
    if chal_n < min_samples:
        rec = "INSUFFICIENT_DATA"
        reason = f"Challenger has only {chal_n} samples (need {min_samples}+)"
    elif chal_acc > champ_acc + 0.02:
        rec = "SWITCH_RECOMMENDED"
        reason = f"Challenger accuracy {chal_acc:.2%} > Champion {champ_acc:.2%} + 2%"
    elif champ_acc > chal_acc + 0.02:
        rec = "KEEP_CHAMPION"
        reason = f"Champion accuracy {champ_acc:.2%} > Challenger {chal_acc:.2%} + 2%"
    else:
        rec = "KEEP_CHAMPION"
        reason = "No significant difference"

    print(f"\n  Recommendation: {rec}")
    print(f"  Reason: {reason}")

    return 0


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _update_config_version(key: str, value: str) -> None:
    """Atomically update a version string in config.py."""
    config_path = Path("config.py")
    if not config_path.exists():
        raise FileNotFoundError("config.py not found")

    content = config_path.read_text()

    # Find and replace the line
    import re
    pattern = rf'^({key}\s*=\s*)(["\']).*?\2'
    replacement = rf'\g<1>"{value}"'
    new_content, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)

    if count == 0:
        raise ValueError(f"{key} not found in config.py")

    # Atomic write
    fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
    try:
        os.write(fd, new_content.encode())
        os.close(fd)
        os.replace(tmp_path, config_path)
    except Exception:
        os.close(fd)
        os.unlink(tmp_path)
        raise


def _read_challenger_list() -> list[str]:
    """Read CHALLENGER_MODELS from config.py."""
    config_path = Path("config.py")
    if not config_path.exists():
        return []

    import re
    content = config_path.read_text()
    match = re.search(r'CHALLENGER_MODELS\s*=\s*(\[.*?\])', content, re.DOTALL)
    if not match:
        return []

    try:
        return json.loads(match.group(1).replace("'", '"'))
    except (json.JSONDecodeError, ValueError):
        return []


def _update_config_challenger_list(versions: list[str]) -> None:
    """Atomically update CHALLENGER_MODELS in config.py."""
    config_path = Path("config.py")
    if not config_path.exists():
        raise FileNotFoundError("config.py not found")

    content = config_path.read_text()

    import re
    list_str = json.dumps(versions)
    pattern = r'CHALLENGER_MODELS\s*=\s*\[.*?\]'
    new_content, count = re.subn(
        pattern, f"CHALLENGER_MODELS = {list_str}", content, flags=re.DOTALL
    )

    if count == 0:
        # Append if not found
        new_content = content + f"\nCHALLENGER_MODELS = {list_str}\n"

    fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
    try:
        os.write(fd, new_content.encode())
        os.close(fd)
        os.replace(tmp_path, config_path)
    except Exception:
        os.close(fd)
        os.unlink(tmp_path)
        raise
