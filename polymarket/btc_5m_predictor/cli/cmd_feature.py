"""Feature engineering commands: validate, explore."""

import argparse


def run(args: argparse.Namespace) -> int:
    if args.feature_action is None:
        print("Usage: btc feature {validate|explore}")
        return 1

    if args.feature_action == "validate":
        return _validate(args.name)
    elif args.feature_action == "explore":
        return _explore(getattr(args, "category", None))

    return 1


def _validate(name: str) -> int:
    print(f"[TODO] btc feature validate {name} - not yet implemented")
    return 0


def _explore(category: str | None) -> int:
    print(f"[TODO] btc feature explore --category {category} - not yet implemented")
    return 0
