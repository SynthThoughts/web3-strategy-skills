"""Monitoring commands: drift, diagnose, retrain-check."""

import argparse


def run(args: argparse.Namespace) -> int:
    if args.monitor_action is None:
        print("Usage: btc monitor {drift|diagnose|retrain-check}")
        return 1

    if args.monitor_action == "drift":
        return _drift()
    elif args.monitor_action == "diagnose":
        return _diagnose()
    elif args.monitor_action == "retrain-check":
        return _retrain_check()

    return 1


def _drift() -> int:
    print("[TODO] btc monitor drift - not yet implemented")
    return 0


def _diagnose() -> int:
    print("[TODO] btc monitor diagnose - not yet implemented")
    return 0


def _retrain_check() -> int:
    print("[TODO] btc monitor retrain-check - not yet implemented")
    return 0
