"""Experiment tracking commands: list, compare, explain."""

import argparse


def run(args: argparse.Namespace) -> int:
    if args.experiment_action is None:
        print("Usage: btc experiment {list|compare|explain}")
        return 1

    if args.experiment_action == "list":
        return _list(args)
    elif args.experiment_action == "compare":
        return _compare(args.id1, args.id2, getattr(args, "json", False))
    elif args.experiment_action == "explain":
        return _explain(args.id, getattr(args, "slice", False))

    return 1


def _list(args: argparse.Namespace) -> int:
    print("[TODO] btc experiment list - not yet implemented")
    return 0


def _compare(id1: str, id2: str, as_json: bool) -> int:
    print(f"[TODO] btc experiment compare {id1} {id2} - not yet implemented")
    return 0


def _explain(run_id: str, with_slice: bool) -> int:
    print(f"[TODO] btc experiment explain {run_id} - not yet implemented")
    return 0
