"""The pytest selections: one command for the sections the make targets named.

Selection has two axes: a directory names a component (tests/evalhub), a marker names a
section within it (evalhub, benchmark, adapters, generation). `--select` takes any pytest
selection, so a section, a directory, a node id or -k all work. Three sections are their
own because a full run of them is slow: the adapter checks each submit a real benchmark
that takes minutes, the image-pull-grace check waits out the operator's ten-minute
image-pull grace, and the isvc-free checks restart both operators to prove they start
with no serving API. `tas-payload` is the other side of that one - it installs the
serving API and runs after `isvc-free`, in the order the report keeps.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import click

from harness import resources
from harness.config import Settings, is_deployed_record, record_values
from harness.shell import HarnessError, run

SECTIONS = (
    "standard",
    "all",
    "adapters",
    "adapters-atif",
    "evalhub",
    "image-pull-grace",
    "generation",
    "isvc-free",
    "lmeval",
    "tas-payload",
)


def section_marker(section: str, settings: Settings) -> str | None:
    """The marker expression a section runs, or None for no restriction."""
    adapters = settings.adapters_marker
    match section:
        case "standard":
            return f"not {adapters} and not image_pull_grace"
        case "all":
            return None
        case "adapters":
            # Every adapter check, ATIF included: a section named for the adapters that
            # silently dropped one would make `-k <adapter>` - the way every other adapter
            # is narrowed - collect nothing for it. The ATIF checks are the slow ones, so a
            # run that wants the others pays for `-k 'not atif'`, and `adapters-atif` remains
            # the way to run that block alone into its own report.
            return adapters
        case "adapters-atif":
            # The ATIF checks deploy MLflow and a judge, and each job is minutes of a
            # Kind cluster's time, so they are never part of the default run either.
            # The marker is `atif`: `-m` reads a hyphen as subtraction.
            return "atif"
        case "evalhub":
            return f"evalhub and not {adapters} and not image_pull_grace"
        case "image-pull-grace":
            # The grace case waits out the operator's ten-minute image-pull grace,
            # so it is never part of the default run either.
            return "image_pull_grace"
        case "generation":
            return "evalhub and generation"
        case "isvc-free":
            return "isvc_free"
        case "tas-payload":
            return "tas_payload"
        case "lmeval":
            return "lmeval"
    raise HarnessError(f"unknown section {section!r}; choose one of {', '.join(SECTIONS)}")


def pytest_arguments(
    settings: Settings, *, section: str, marker: str | None, select: tuple[str, ...], report: str | None
) -> tuple[list[str], Path]:
    """The pytest command line for a selection, and the report it writes."""
    tokens = [token for value in select for token in shlex.split(value)]
    if select and not tokens:
        raise HarnessError(
            "--select must name a pytest selection, for example --select '-m generation' or --select tests/evalhub"
        )
    # --select names its own selection, so the section marker would narrow it behind the
    # caller's back; an explicit --marker still applies.
    expression = marker or (None if tokens else section_marker(section, settings))
    section_report = {
        "adapters": settings.adapters_report_file,
        "adapters-atif": settings.adapters_atif_report_file,
    }
    target = Path(report or (section_report.get(section) if not tokens else None) or settings.report_file)

    root = resources.tests_root()
    arguments = [sys.executable, "-m", "pytest", *(_inside(root, token) for token in tokens)]
    if not any(_names_the_suite(token) for token in tokens):
        arguments.append(str(root))
    if expression:
        arguments += ["-m", expression]
    arguments += shlex.split(settings.pytest_args)
    arguments += [f"--html={target}", "--self-contained-html"]
    # Named outright, so a pyproject.toml in the caller's directory cannot decide the
    # markers, addopts or rootdir of someone else's suite.
    arguments += ["-c", str(resources.pytest_config())]
    if resources.from_package():
        # Nothing is written into the installed copy.
        arguments += ["-p", "no:cacheprovider"]
    return arguments, target


def _names_the_suite(token: str) -> bool:
    return token == resources.TESTS or token.startswith(f"{resources.TESTS}/")


def _inside(root: Path, token: str) -> str:
    """A pytest argument, with a `tests/...` path resolved inside this run's suite."""
    if not _names_the_suite(token):
        return token
    if token == resources.TESTS:
        return str(root)
    return str(root / token[len(resources.TESTS) + 1 :])


def settings_for_run(settings: Settings) -> Settings:
    """The settings a run collects with, taking images from the record this deployment stamped.

    `up --atif-img X` names an adapter image once: `deploy` records what it brought up, and
    a later `test` reads it back here, so the section verifies the image the build produced
    without being told it again. An image the caller names explicitly still wins - the
    record fills gaps, it does not overrule the command line.
    """
    path = settings.images_env_path
    if is_deployed_record(path):
        return settings.with_image_record(record_values(path))
    return settings


def run_tests(
    settings: Settings,
    *,
    section: str,
    marker: str | None,
    select: tuple[str, ...],
    report: str | None,
    keep_resources: bool,
) -> int:
    """Run the selection, leave the resources when asked, and say where the report went."""
    settings = settings_for_run(settings)
    arguments, target = pytest_arguments(settings, section=section, marker=marker, select=select, report=report)
    settings.with_updates(keep_resources=keep_resources).export()
    result = run(arguments, check=False)
    click.echo(f"HTML report: {target.resolve()}")
    return result.returncode


__all__ = ["SECTIONS", "pytest_arguments", "run_tests", "section_marker", "settings_for_run"]
