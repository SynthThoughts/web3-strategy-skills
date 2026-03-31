"""Data management commands: status, fetch, sync, health, validate."""

import argparse


def run(args: argparse.Namespace) -> int:
    if args.data_action is None:
        print("Usage: btc data {status|fetch|sync|health|validate}")
        return 1

    if args.data_action == "status":
        return _status()
    elif args.data_action == "fetch":
        return _fetch(args.source, args.days)
    elif args.data_action == "sync":
        return _sync(getattr(args, "full", False))
    elif args.data_action == "health":
        return _health()
    elif args.data_action == "validate":
        return _validate()

    return 1


def _status() -> int:
    print("[TODO] btc data status - not yet implemented")
    return 0


def _fetch(source: str, days: int | None) -> int:
    print(f"[TODO] btc data fetch --source {source} --days {days} - not yet implemented")
    return 0


def _sync(full: bool) -> int:
    print(f"[TODO] btc data sync {'--full' if full else ''} - not yet implemented")
    return 0


def _health() -> int:
    print("[TODO] btc data health - not yet implemented")
    return 0


def _validate() -> int:
    print("[TODO] btc data validate - not yet implemented")
    return 0
