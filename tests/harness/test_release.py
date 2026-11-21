"""Release gating: parsing, comparison, the range decision, and the wiring that applies it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from harness.config import KEEP_RESOURCES_VAR, SETTINGS, Settings
from utilities import release as release_module
from utilities.release import applies, compare, declared_release, describe, parse

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("3.5", (3, 5)),
        ("3.6.1", (3, 6, 1)),
        ("v3.6", (3, 6)),
        ("3.6-ea.2", (3, 6)),
        ("3.6.0-rc-1.2.3", (3, 6, 0)),
        (" 3.10 ", (3, 10)),
    ],
)
def test_a_release_parses_component_by_component(declared, expected):
    assert parse(declared) == expected


@pytest.mark.parametrize("declared", ["", "main", "3.x", "latest", "release-3.6", "rhoai-3.6"])
def test_a_spelling_that_is_not_a_release_is_rejected(declared):
    with pytest.raises(ValueError, match="is not a release version"):
        parse(declared)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("3.5", "3.6", -1),
        ("3.6", "3.5", 1),
        ("3.6", "3.6.0", 0),
        ("3.6.1", "3.6", 1),
        # Component-wise, so 3.10 is newer than 3.9 and not a string comparison.
        ("3.10", "3.9", 1),
        ("3.6-ea.2", "3.6", 0),
    ],
)
def test_releases_compare_component_by_component(left, right, expected):
    assert compare(left, right) == expected


def test_an_undeclared_release_gates_nothing():
    assert applies(min="3.6", run=None)
    assert applies(max="3.5", run=None)


@pytest.mark.parametrize(
    ("run", "minimum", "maximum", "expected"),
    [
        ("3.6", "3.6", None, True),
        ("3.5", "3.6", None, False),
        ("3.5", None, "3.5", True),
        ("3.6", None, "3.5", False),
        ("3.6", "3.5", "3.6", True),
        ("3.7", "3.5", "3.6", False),
        ("3.6.1", None, "3.6", False),
        ("3.6", None, None, True),
    ],
)
def test_bounds_are_inclusive(run, minimum, maximum, expected):
    assert applies(min=minimum, max=maximum, run=run) is expected


@pytest.mark.parametrize(
    ("minimum", "maximum", "expected"),
    [
        ("3.6", None, "3.6 and newer"),
        (None, "3.5", "3.5 and older"),
        ("3.5", "3.6", "3.5 to 3.6"),
        (None, None, "every release"),
    ],
)
def test_a_range_is_described_for_the_skip_reason(minimum, maximum, expected):
    assert describe(min=minimum, max=maximum) == expected


def test_release_needs_a_bound():
    with pytest.raises(ValueError, match="at least one of min or max"):
        release_module.release()


def test_release_marks_the_check_with_its_range():
    mark = release_module.release(min="3.6")
    assert mark.name == release_module.RELEASE_MARKER
    assert mark.kwargs == {"min": "3.6", "max": None}


def test_the_declared_release_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPERATOR_RELEASE", "3.6")
    assert declared_release() == "3.6"
    monkeypatch.setenv("OPERATOR_RELEASE", "   ")
    assert declared_release() is None
    monkeypatch.delenv("OPERATOR_RELEASE")
    assert declared_release() is None


def test_the_marker_is_registered():
    """`--strict-markers` would fail the run if pyproject did not declare it."""
    configuration = (REPO_ROOT / "pyproject.toml").read_text()
    assert f'"{release_module.RELEASE_MARKER}: ' in configuration


def test_the_harness_takes_the_release_and_exports_it(monkeypatch):
    settings = Settings.from_params({"release": "3.6"})
    assert settings.release == "3.6"
    # `export` writes every setting into the environment, not only the release, so each
    # name is registered first. `setenv` is what records a name that was not set
    # (`delenv(raising=False)` records nothing for one), so teardown puts every original
    # back - the value, or the absence.
    for setting in SETTINGS:
        monkeypatch.setenv(setting.envvar, "")
    monkeypatch.setenv(KEEP_RESOURCES_VAR, "")
    settings.export()
    import os

    assert os.environ["OPERATOR_RELEASE"] == "3.6"
    assert "release" in settings.explicit
    assert Settings().release == ""


def test_a_declared_release_skips_checks_outside_their_range(tmp_path):
    """The wiring, end to end: the skip happens at collection, so no cluster is involved."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(REPO_ROOT / "tests" / "test_trustyai_module.py"),
            "--release",
            "3.5",
            "-q",
            "--no-header",
            "-rs",
            "-p",
            "no:cacheprovider",
            f"--html={tmp_path / 'report.html'}",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "applies to operator releases 3.6 and newer; this run targets 3.5" in result.stdout


def test_a_bad_declaration_fails_the_run_rather_than_one_check(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(REPO_ROOT / "tests" / "test_trustyai_module.py"),
            "--release",
            "main",
            "-q",
            "--no-header",
            f"--html={tmp_path / 'report.html'}",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "is not a release version" in result.stdout + result.stderr
