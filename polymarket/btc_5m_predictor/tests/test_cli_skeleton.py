"""Tests for CLI skeleton: parser, subcommands, dispatch."""

import pytest

from cli import main


def test_no_args_prints_help(capsys):
    """btc with no args prints help and returns 0."""
    ret = main([])
    assert ret == 0
    out = capsys.readouterr().out
    assert "usage:" in out.lower() or "btc" in out.lower()


def test_unknown_command():
    """btc with unknown command exits with error (argparse SystemExit)."""
    with pytest.raises(SystemExit) as exc_info:
        main(["nonexistent"])
    assert exc_info.value.code == 2  # argparse error exit code


def test_data_no_action(capsys):
    """btc data with no action prints usage hint."""
    ret = main(["data"])
    assert ret == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_data_status(capsys):
    """btc data status returns 0 (stub)."""
    ret = main(["data", "status"])
    assert ret == 0
    assert "todo" in capsys.readouterr().out.lower()


def test_feature_no_action(capsys):
    """btc feature with no action prints usage hint."""
    ret = main(["feature"])
    assert ret == 1


def test_feature_validate(capsys):
    """btc feature validate returns 0 (stub)."""
    ret = main(["feature", "validate", "test_feature"])
    assert ret == 0


def test_train(capsys):
    """btc train returns 0 (stub)."""
    ret = main(["train"])
    assert ret == 0


def test_experiment_no_action(capsys):
    """btc experiment with no action prints usage hint."""
    ret = main(["experiment"])
    assert ret == 1


def test_experiment_list(capsys):
    """btc experiment list returns 0 (stub)."""
    ret = main(["experiment", "list"])
    assert ret == 0


def test_experiment_compare(capsys):
    """btc experiment compare requires two IDs."""
    ret = main(["experiment", "compare", "run_a", "run_b"])
    assert ret == 0


def test_deploy_no_action(capsys):
    """btc deploy with no action prints usage hint."""
    ret = main(["deploy"])
    assert ret == 1


def test_deploy_promote(capsys):
    """btc deploy promote returns 0 (stub)."""
    ret = main(["deploy", "promote", "run_123"])
    assert ret == 0


def test_monitor_no_action(capsys):
    """btc monitor with no action prints usage hint."""
    ret = main(["monitor"])
    assert ret == 1


def test_monitor_drift(capsys):
    """btc monitor drift returns 0 (stub)."""
    ret = main(["monitor", "drift"])
    assert ret == 0


def test_all_subcommands_dispatch():
    """Every top-level subcommand dispatches without import errors."""
    commands = [
        ["data", "status"],
        ["feature", "validate", "x"],
        ["train"],
        ["experiment", "list"],
        ["deploy", "promote", "x"],
        ["monitor", "drift"],
    ]
    for argv in commands:
        ret = main(argv)
        assert ret == 0, f"Command {argv} returned {ret}"
