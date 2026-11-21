"""Which operator layout a checkout has, and whether this run can deploy it.

The module operator was split out of the operator for 3.6: a modular checkout has
`trustyai-operator-module/` and the module operator renders the workload operator from
the manifests baked into its image, while a pre-module checkout (3.5 and earlier) has
one operator that is deployed directly.

The layout follows the checkout, because only what the tree contains can be built and
deployed. `--operator-arch` is an assertion, not a preference: naming a layout the
checkout does not have stops the run with what is missing, which is how a stale or
half-synced checkout gets caught before an image is built. `--release` is the release
under test and decides which checks run, not which layout is deployed; when the two
disagree the run says so.
"""

from __future__ import annotations

from pathlib import Path

from harness.config import Settings
from harness.shell import HarnessError, warn

ARCH_MODULAR = "modular"
ARCH_SINGLE = "single"
ARCHITECTURES = (ARCH_MODULAR, ARCH_SINGLE)

MODULE_DIR = "trustyai-operator-module"

# The overlays a pre-module checkout declares its one operator in, in order of preference.
SINGLE_OVERLAYS = ("config/overlays/rhoai", "config/overlays/odh", "config/default")

# The first release whose operator split the module operator out.
MODULE_OPERATOR_MIN_VERSION = "3.6"


def operator_repo(settings: Settings) -> Path:
    """The checkout, as a path; nothing about it is checked here."""
    return Path(settings.operator_repo).expanduser()


def module_tree(settings: Settings) -> Path:
    """Where a modular checkout keeps the module operator."""
    return operator_repo(settings) / MODULE_DIR


def operator_arch(settings: Settings) -> str:
    """The layout this run deploys: the one declared, or the one the checkout has."""
    if settings.operator_arch:
        return settings.operator_arch
    return ARCH_MODULAR if module_tree(settings).is_dir() else ARCH_SINGLE


def arch_source(settings: Settings) -> str:
    """Why that layout, in a line for the output."""
    if settings.operator_arch:
        return f"OPERATOR_ARCH={settings.operator_arch}"
    if operator_arch(settings) == ARCH_MODULAR:
        return f"the checkout, which has {MODULE_DIR}/"
    return f"the checkout, which has no {MODULE_DIR}/ (pre-module)"


def deploys_module_operator(settings: Settings) -> bool:
    return operator_arch(settings) == ARCH_MODULAR


def operator_images(settings: Settings) -> list[str]:
    """The images this layout builds: the workload operator always, the module operator when it has one."""
    images = [settings.workload_img]
    if deploys_module_operator(settings):
        images.append(settings.module_img)
    return images


def single_operator_overlay(settings: Settings) -> Path:
    """The overlay a pre-module checkout declares its operator in."""
    for overlay in SINGLE_OVERLAYS:
        candidate = operator_repo(settings) / overlay
        if (candidate / "kustomization.yaml").is_file():
            return candidate
    raise HarnessError(
        f"{settings.operator_repo} has no {', '.join(SINGLE_OVERLAYS)} to render the operator from "
        f"({arch_source(settings)})"
    )


def release_disagrees(settings: Settings) -> str:
    """What to say when the release under test contradicts the layout, or "" when it does not."""
    if not settings.release:
        return ""
    from utilities.release import compare

    try:
        modular_release = compare(settings.release, MODULE_OPERATOR_MIN_VERSION) >= 0
    except ValueError as exc:
        raise HarnessError(f"--release: {exc}") from exc
    if modular_release == deploys_module_operator(settings):
        return ""
    return (
        f"--release {settings.release} says "
        f"{'a release with' if modular_release else 'a release before'} the module operator, but "
        f"{arch_source(settings)}; the layout follows the checkout"
    )


def require_operator_repo(settings: Settings) -> Path:
    """Check the checkout is one, has the layout this run deploys, and warn when --release disagrees."""
    if not settings.operator_repo:
        raise HarnessError(
            "OPERATOR_REPO must point at a trustyai-service-operator checkout, "
            "for example --operator-repo /path/to/trustyai-service-operator"
        )
    repo = operator_repo(settings)
    if not repo.is_dir():
        raise HarnessError(f"OPERATOR_REPO={repo} does not exist")
    if not (repo / "Dockerfile").is_file():
        raise HarnessError(f"{repo} has no Dockerfile; it does not look like a trustyai-service-operator checkout")

    require_layout(settings)
    contradiction = release_disagrees(settings)
    if contradiction:
        warn(contradiction)
    return repo


def require_layout(settings: Settings) -> None:
    """Stop when the checkout and the layout do not go together."""
    tree = module_tree(settings)
    if operator_arch(settings) == ARCH_MODULAR:
        if not tree.is_dir():
            raise HarnessError(
                f"OPERATOR_ARCH={ARCH_MODULAR} but {settings.operator_repo} has no {MODULE_DIR}/; "
                "this is a pre-module checkout, so leave --operator-arch unset or pass single"
            )
        if not (tree / "Dockerfile").is_file():
            raise HarnessError(
                f"{settings.operator_repo}/{MODULE_DIR}/ has no Dockerfile; the checkout looks half-synced"
            )
        return

    if tree.is_dir() and settings.operator_arch:
        raise HarnessError(
            f"OPERATOR_ARCH={ARCH_SINGLE} but {settings.operator_repo} has {MODULE_DIR}/; "
            "the single layout is for a pre-module checkout"
        )
    single_operator_overlay(settings)


__all__ = [
    "ARCHITECTURES",
    "ARCH_MODULAR",
    "ARCH_SINGLE",
    "MODULE_DIR",
    "MODULE_OPERATOR_MIN_VERSION",
    "SINGLE_OVERLAYS",
    "arch_source",
    "deploys_module_operator",
    "module_tree",
    "operator_arch",
    "operator_images",
    "operator_repo",
    "release_disagrees",
    "require_layout",
    "require_operator_repo",
    "single_operator_overlay",
]
