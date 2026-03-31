"""Deployment commands: promote, shadow, compare."""

import argparse


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
    print(f"[TODO] btc deploy promote {run_id} - not yet implemented")
    return 0


def _shadow(args: argparse.Namespace) -> int:
    action = getattr(args, "shadow_action", None)
    if action is None:
        print("Usage: btc deploy shadow {add|remove|list}")
        return 1
    print(f"[TODO] btc deploy shadow {action} - not yet implemented")
    return 0


def _compare() -> int:
    print("[TODO] btc deploy compare - not yet implemented")
    return 0
