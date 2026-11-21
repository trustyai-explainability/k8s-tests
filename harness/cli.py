"""The command line: the harness commands, one subcommand per step.

Every setting is an option and an environment variable of the same name, so
`OPERATOR_REPO=... k8s-tests up` and `k8s-tests up --operator-repo ...` are the same run,
and the checks still read the values from the environment.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import click

from harness import cluster as cluster_ops
from harness import deploy as deploy_ops
from harness import images as image_ops
from harness import resources
from harness import tests as test_ops
from harness.config import KEEP_RESOURCES_VAR, ROOT_VAR, SETTINGS, Setting, Settings, repository_root
from harness.shell import HarnessError, info, run

PROG_NAME = "k8s-tests"

EPILOG = """\
Settings are read from the environment too; --help shows the variable for each.
The harness stops at the operators: `up` leaves the stack running and `test` creates
the operands it checks. Relative paths resolve from the directory the command runs in.
"""

# The settings whose value is a filesystem path, and how each one completes.
PATH_KINDS: dict[str, dict[str, bool]] = {
    "dir": {"exists": False, "file_okay": False, "dir_okay": True},
    "file": {"exists": False, "file_okay": True, "dir_okay": False},
}


class UnlistedOption(click.Option):
    """An option a command accepts without advertising it: it completes, it does not print."""

    def get_help_record(self, ctx: click.Context) -> tuple[str, str] | None:
        return None


def complete_var(prog_name: str = PROG_NAME) -> str:
    return f"_{prog_name.replace('-', '_').replace('.', '_')}_COMPLETE".upper()


def _setting_param(setting: Setting, *, hidden: bool = False) -> click.Option:
    """One command-line option for one declared setting.

    The group's copy reads the environment variable; a command's copy deliberately does
    not, because the group has already exported what it resolved and a command that read
    the variable again would see its own run's values as defaults.
    """
    kwargs: dict[str, object] = {"help": setting.help}
    if setting.kind in PATH_KINDS:
        kwargs["type"] = click.Path(**PATH_KINDS[setting.kind])
    factory: type[click.Option] = click.Option
    if setting.kind == "flag":
        kwargs["is_flag"] = True
    if hidden:
        kwargs["default"] = None
        factory = UnlistedOption
    else:
        kwargs.update(envvar=setting.envvar, show_envvar=True)
        if setting.kind == "choice":
            kwargs.update(type=click.Choice(setting.choices), default=None)
        elif setting.kind == "flag":
            kwargs.update(default=False)
        else:
            kwargs.update(default=setting.default or None, show_default=bool(setting.default))
    return factory([setting.option], **kwargs)


def _setting_params(hidden: bool = False) -> list[click.Option]:
    return [_setting_param(setting, hidden=hidden) for setting in SETTINGS]


class SettingsCommand(click.Command):
    """A command that takes the settings too, so a flag works on either side of its name."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.params = [*self.params, *_setting_params(hidden=True)]

    def invoke(self, ctx: click.Context) -> object:
        overrides = {setting.field: ctx.params.pop(setting.field, None) for setting in SETTINGS}
        given = {name: value for name, value in overrides.items() if value is not None}
        root = ctx.find_root()
        current = root.obj if isinstance(root.obj, Settings) else Settings()
        merged = current.with_updates(**given) if given else current
        root.obj = ctx.obj = merged
        merged.export()
        return super().invoke(ctx)


class HarnessGroup(click.Group):
    command_class = SettingsCommand


def _drop_empty_environment() -> None:
    """Treat an empty variable as unset rather than as a value."""
    for setting in SETTINGS:
        if setting.envvar in os.environ and not os.environ[setting.envvar].strip():
            del os.environ[setting.envvar]
    if not os.environ.get(KEEP_RESOURCES_VAR, "").strip():
        os.environ.pop(KEEP_RESOURCES_VAR, None)


@click.group(
    cls=HarnessGroup,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 110},
    epilog=EPILOG,
    params=_setting_params(),
)
@click.option("-v", "--verbose", count=True, help="log the shell commands the harness runs")
@click.pass_context
def cli(ctx: click.Context, verbose: int, **params: object) -> None:
    """Kind harness and Kubernetes-level checks for the TrustyAI components.

    `up` creates a Kind cluster, builds the operator images from a checkout, loads them,
    deploys the stack and waits for it. `test` creates the operands and checks them.
    """
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="==> %(message)s", stream=sys.stderr)
    settings = Settings.from_params(params)
    settings.export()
    ctx.obj = settings
    if verbose:
        click.echo(f"==> reading {resources.describe()}", err=True)


@cli.command()
@click.pass_obj
def cluster(settings: Settings) -> None:
    """Create the Kind cluster (idempotent), and wait for its nodes."""
    cluster_ops.create(settings)


@cli.command()
@click.pass_obj
def down(settings: Settings) -> None:
    """Delete the Kind cluster."""
    cluster_ops.delete(settings)


@cli.command("check-operator-repo")
@click.pass_obj
def check_operator_repo(settings: Settings) -> None:
    """Check OPERATOR_REPO looks like a trustyai-service-operator checkout."""
    image_ops.check_operator_repo(settings)


@cli.command()
@click.pass_obj
def images(settings: Settings) -> None:
    """Build the workload and module operator images from OPERATOR_REPO."""
    image_ops.build_load_path_images(settings)


@cli.command("images-all")
@click.pass_obj
def images_all(settings: Settings) -> None:
    """Build every source image and push it to the local registry."""
    image_ops.images_all(settings)


@cli.command()
@click.pass_obj
def load(settings: Settings) -> None:
    """Load the built operator images into the Kind cluster."""
    image_ops.load(settings)


@cli.command()
@click.pass_obj
def deploy(settings: Settings) -> None:
    """Deploy the module operator, its module resource, and the workload operator."""
    deploy_ops.deploy(settings)


@cli.command("deploy-registry")
@click.pass_obj
def deploy_registry(settings: Settings) -> None:
    """Deploy with the images images-all pushed to the local registry."""
    deploy_ops.registry_deploy(settings)


@cli.command()
@click.pass_obj
def up(settings: Settings) -> None:
    """Create the cluster, build, load and deploy, and wait for the operators."""
    image_ops.check_operator_repo(settings)
    cluster_ops.create(settings)
    image_ops.build_load_path_images(settings)
    image_ops.load(settings)
    deploy_ops.deploy(settings)


@cli.command("up-registry")
@click.pass_obj
def up_registry(settings: Settings) -> None:
    """Create the cluster, build and push every image, and deploy those images."""
    deploy_ops.registry_up(settings)


@cli.command()
@click.pass_obj
def clean(settings: Settings) -> None:
    """Delete the built images, the local registry and the HTML report (leaves the cluster)."""
    image_ops.clean(settings)


@cli.command()
@click.pass_obj
def status(settings: Settings) -> None:
    """Show the module, the operators, the pods, EvalHub and the emulator."""
    cluster_ops.status(settings)


@cli.command()
@click.pass_obj
def logs(settings: Settings) -> None:
    """Tail the operator logs."""
    cluster_ops.logs(settings)


@cli.command()
@click.option(
    "--section",
    type=click.Choice(test_ops.SECTIONS),
    default="standard",
    show_default=True,
    help="which section of the checks to run",
)
@click.option("-m", "--marker", metavar="EXPRESSION", help="a pytest marker expression, used instead of the section's")
@click.option(
    "--select",
    multiple=True,
    metavar="ARGS",
    help="a pytest selection, shell-split, for example '-m generation' or tests/evalhub",
)
@click.option("--report", metavar="PATH", help="HTML report to write; defaults per section")
@click.option(
    "--keep-resources/--no-keep-resources",
    default=False,
    envvar=KEEP_RESOURCES_VAR,
    show_envvar=True,
    help="leave the namespaces, the LLM emulator and the job resources on the cluster for inspection",
)
@click.pass_obj
def test(
    settings: Settings,
    section: str,
    marker: str | None,
    select: tuple[str, ...],
    report: str | None,
    keep_resources: bool,
) -> None:
    """Run the checks, and write an HTML report.

    The default section runs every check except the adapters; `--section adapters` runs
    only those, into their own report, because each one submits a real benchmark.
    """
    sys.exit(
        test_ops.run_tests(
            settings, section=section, marker=marker, select=select, report=report, keep_resources=keep_resources
        )
    )


def _ruff() -> str:
    """The ruff in the environment the CLI runs in, so a bare `ruff` cannot shadow it."""
    executable = Path(sys.executable).with_name("ruff")
    return str(executable) if executable.exists() else "ruff"


@cli.command()
def lint() -> None:
    """Lint and format-check this project's own sources."""
    root = needs_checkout()
    run([_ruff(), "check", str(root)])
    run([_ruff(), "format", "--check", str(root)])


@cli.command()
def fmt() -> None:
    """Format and autofix this project's own sources."""
    root = needs_checkout()
    run([_ruff(), "format", str(root)])
    run([_ruff(), "check", "--fix", str(root)])


LINT_WITHOUT_CHECKOUT = f"""\
lint and fmt check this project's own sources, and this installation has no checkout of
them: a wheel ships the harness, not the repository it was built from. They need one:

    {ROOT_VAR}=/path/to/k8s-tests k8s-tests lint
"""


def needs_checkout() -> Path:
    """The checkout lint and fmt work on, or a refusal that says where to point them."""
    root = repository_root()
    if root is None:
        raise HarnessError(LINT_WITHOUT_CHECKOUT)
    return root


@cli.command()
@click.argument("shell", type=click.Choice(("bash", "zsh", "fish")))
def completion(shell: str) -> None:
    """Print the shell completion script for SHELL.

    \b
    bash   source <(k8s-tests completion bash)
    zsh    k8s-tests completion zsh > "${fpath[1]}/_k8s-tests"     # then: compinit
    fish   k8s-tests completion fish > ~/.config/fish/completions/k8s-tests.fish
    """
    from click.shell_completion import BashComplete, FishComplete, ZshComplete

    shells = {"bash": BashComplete, "zsh": ZshComplete, "fish": FishComplete}
    script = shells[shell](cli=cli, ctx_args={}, prog_name=PROG_NAME, complete_var=complete_var()).source()
    click.echo(script)


def main() -> None:
    """Entry point. Nothing changes directory: what a run reads is resolved where it is read."""
    _drop_empty_environment()
    cli()


__all__ = ["HarnessError", "cli", "info", "main"]
