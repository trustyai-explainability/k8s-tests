"""Exercise pytest collection without loading Kubernetes clients."""

import subprocess
import sys

import pytest
from click.testing import CliRunner

from harness import cli, resources, tests


@pytest.mark.parametrize("flags", [(), ("--chaos",)])
def test_chaos_collection_is_opt_in_and_exclusive(flags):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "--collect-only",
            "-q",
            *flags,
            "-c",
            str(resources.pytest_config()),
            str(resources.tests_root()),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    nodes = [line for line in result.stdout.splitlines() if "::" in line]
    assert nodes
    assert all(("/chaos/" in node) == bool(flags) for node in nodes)


@pytest.mark.parametrize("chaos", [False, True])
def test_collection_with_a_stale_install(chaos):
    # Simulate a standalone tool installed before Hypothesis was a dependency.
    script = """
import sys

sys.modules['hypothesis'] = None
import pytest
sys.exit(pytest.main(sys.argv[1:]))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            "-p",
            "no:hypothesispytest",
            "--assert=plain",
            "-o",
            "addopts=",
            "--collect-only",
            "-q",
            "-c",
            str(resources.pytest_config()),
            str(resources.tests_root()),
            *(["--chaos"] if chaos else []),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if chaos:
        assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
        assert "uv tool install --force ." in result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "/chaos/" not in result.stdout


def test_cli_forwards_chaos_to_pytest(monkeypatch):
    from types import SimpleNamespace

    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(tests, "run", run)
    result = CliRunner().invoke(cli.cli, ["test", "--chaos"])
    assert result.exit_code == 0, result.output
    assert "--chaos" in commands[0]
    assert "-m" not in commands[0][3:]
