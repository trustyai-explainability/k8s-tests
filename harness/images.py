"""Building the images, loading them into Kind, pushing them to the local registry.

`images` builds the two operator images and loads them into the node. `images-all`
builds every image the suite can build from source and pushes it to the local registry,
recording the references in IMAGES_ENV_FILE for `deploy-registry` to read back.
"""

from __future__ import annotations

import filecmp
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from pathlib import Path

from harness import registry
from harness.arch import MODULE_DIR, deploys_module_operator, operator_images, operator_repo, require_operator_repo
from harness.config import Settings, record_lines
from harness.engine import (
    build,
    container_engine,
    container_state,
    image_present,
    platform,
    push,
    reachable_engine,
    remove,
    save,
)
from harness.shell import HarnessError, environment, info, output, run, warn

LMES_DOCKERFILE = "Dockerfile.lmes-job"

#: The params.env key each provider image is rendered from. A provider the module operator
#: resolves from a RELATED_IMAGE_* variable needs no entry: the platform variable already
#: reaches it. A provider without one is rendered from the template baked into the module
#: image, so its value is fixed when that image is built - see `staged_module_context`.
PROVIDER_PARAMS_KEYS = {"atif": "evalhub-provider-atif-image"}


def provider_params_key(adapter: str) -> str:
    """The params.env key a provider image is rendered from."""
    return PROVIDER_PARAMS_KEYS.get(adapter, f"evalhub-provider-{adapter.replace('_', '-')}-image")


#: Images a run can name that this path does not build. They are loaded when the engine's
#: store holds them, so a locally built or pulled operand runs on the node as named, and left
#: to the cluster otherwise - a remote reference needs no help from here. The default of each
#: field is read from `Settings`, so an unset field is not mistaken for a named image.
NAMED_IMAGE_FIELDS = {
    "evalhub_img": "EvalHub",
    "trustyai_service_img": "TrustyAI service",
    "kube_rbac_proxy_img": "kube-rbac-proxy",
    "lmes_img": "lm_evaluation_harness adapter",
    "mlflow_img": "MLflow tracking server",
    "emulator_img": "LLM emulator",
}

_SETTINGS_DEFAULTS = {field.name: field.default for field in dataclass_fields(Settings)}


def named_images(settings: Settings) -> list[tuple[str, str]]:
    """The other images this run named, as (label, image), skipping any left at its default."""
    named: list[tuple[str, str]] = []
    for field, label in NAMED_IMAGE_FIELDS.items():
        image = getattr(settings, field, "") or ""
        if image and image != _SETTINGS_DEFAULTS.get(field):
            named.append((label, image))
    return named


def unmapped_adapter_overrides(settings: Settings) -> list[tuple[str, str, str]]:
    """The selected adapters whose image has no RELATED_IMAGE_* variable, with that image.

    Each is (params.env key, adapter, image). An adapter the module operator already
    resolves from a platform variable is skipped: setting the variable is enough, and
    rebuilding the module image for it would be work with nothing to show.
    """
    from harness.deploy import RELATED_IMAGES

    mapped = {field for _, field, _ in RELATED_IMAGES}
    overrides: list[tuple[str, str, str]] = []
    for adapter in registry.selected_adapters(settings):
        field = f"{adapter.replace('-', '_')}_img"
        if field in mapped:
            continue
        image = getattr(settings, field, "") or ""
        if image:
            overrides.append((provider_params_key(adapter), adapter, image))
    return overrides


def _rewrite_params(path: Path, values: dict[str, str]) -> bool:
    """Point the keys `values` names at their image, leaving every other line alone."""
    if not path.is_file():
        return False
    lines = path.read_text().splitlines()
    changed = False
    rewritten: list[str] = []
    for line in lines:
        key, separator, _ = line.partition("=")
        if separator and key in values:
            line = f"{key}={values[key]}"
            changed = True
        rewritten.append(line)
    if changed:
        path.write_text("\n".join(rewritten) + "\n")
    return changed


def _relax_pull_policy(path: Path) -> bool:
    """Let a provider use the image the node already holds.

    A provider that ships `image_pull_policy: always` ignores a locally loaded image and
    asks a registry for a tag that may exist only in the engine's store, so a build that
    was loaded rather than pushed could never run.
    """
    if not path.is_file():
        return False
    text = path.read_text()
    rewritten = re.sub(r"(?m)^(\s*)image_pull_policy:\s*always\s*$", r"\1image_pull_policy: if_not_present", text)
    if rewritten == text:
        return False
    path.write_text(rewritten)
    return True


@contextmanager
def staged_module_context(settings: Settings, module: Path, overrides: list[tuple[str, str, str]]) -> Iterator[Path]:
    """Stage the module build context in a temp directory with the providers pointed at this run's images.

    The module image bakes `config/manifests-template`, and the module operator rewrites
    `params.env` on every reconcile only for the keys its own RELATED_IMAGE_* map names. A
    provider outside that map is fixed at build time instead: the staged copy carries the
    image, so the operator renders it from the start, it survives a restart, and nothing
    has to be edited on the running cluster or in the checkout.
    """
    if not overrides:
        yield module
        return
    values = {key: image for key, _, image in overrides}
    with tempfile.TemporaryDirectory(prefix="trustyai-module-") as staging:
        staged = Path(staging) / MODULE_DIR
        shutil.copytree(module, staged)
        template = staged / "config" / "manifests-template"
        touched = [
            path
            for path in (template / "base" / "params.env", *sorted(template.glob("overlays/*/params.env")))
            if _rewrite_params(path, values)
        ]
        if not touched:
            raise HarnessError(
                f"no params.env under {template} carries {', '.join(values)}; "
                "the module template changed shape, so the provider image cannot be staged"
            )
        for _, adapter, image in overrides:
            # A reference the local registry serves is pulled as before; one that exists
            # only in the engine's store has to be found on the node.
            if not image.startswith(f"{settings.registry_host}/"):
                _relax_pull_policy(template / "configmaps" / "evalhub" / f"provider-{adapter}.yaml")
        info(f"staged {MODULE_DIR} for {', '.join(f'{key}={image}' for key, _, image in overrides)}")
        yield staged


def check_operator_repo(settings: Settings) -> None:
    """Report the checkout this run builds and deploys from, and the layout it has."""
    from harness.arch import arch_source, operator_arch

    repo = require_operator_repo(settings)
    info(f"{repo} is a trustyai-service-operator checkout")
    info(f"layout: {operator_arch(settings)} ({arch_source(settings)})")


def trees_differ(left: Path, right: Path) -> bool:
    """Whether two directory trees differ in content or in the files they hold."""
    comparison = filecmp.dircmp(left, right)
    if comparison.left_only or comparison.right_only or comparison.funny_files:
        return True
    _, mismatch, errors = filecmp.cmpfiles(left, right, comparison.common_files, shallow=False)
    if mismatch or errors:
        return True
    return any(trees_differ(left / name, right / name) for name in comparison.common_dirs)


def warn_on_stale_template(settings: Settings) -> None:
    """The module image bakes the workload manifests, so a stale template deploys the previous ones."""
    repo = operator_repo(settings)
    template = repo / MODULE_DIR / "config" / "manifests-template"
    if template.is_dir() and trees_differ(repo / "config", template):
        warn(f"config/ and {MODULE_DIR}/config/manifests-template/ in {repo} differ;")
        warn(f"run 'make -C {repo} sync-trustyai-module-manifests' to refresh the template")


def images(settings: Settings) -> list[str]:
    """The operator images this layout builds."""
    return operator_images(settings)


def build_operators(settings: Settings) -> None:
    """Build the operator images this layout needs from OPERATOR_REPO."""
    repo = require_operator_repo(settings)
    if operator_images(settings) == [settings.workload_img]:
        build(
            settings,
            settings.workload_img,
            repo,
            repo / "Dockerfile",
            "--build-arg",
            f"CI_CONTAINER_VERSION={settings.tag}",
        )
        info(f"built {settings.workload_img} (single-operator layout: there is no module operator image)")
        return

    module = repo / MODULE_DIR
    warn_on_stale_template(settings)
    build(
        settings,
        settings.workload_img,
        repo,
        repo / "Dockerfile",
        "--build-arg",
        f"CI_CONTAINER_VERSION={settings.tag}",
    )
    overrides = unmapped_adapter_overrides(settings)
    with staged_module_context(settings, module, overrides) as context:
        build(settings, settings.module_img, context, context / "Dockerfile", "--build-arg", f"VERSION={settings.tag}")
    info(f"built {settings.workload_img} and {settings.module_img}")


def loadable_adapter_images(settings: Settings) -> list[tuple[str, str]]:
    """The adapter images this run was handed, as (adapter, image).

    `images-all` builds every adapter and pushes it to the registry, which `deploy-registry`
    reads back. The load path has no registry, so an adapter image named on the command line
    is the one to build and put on the node - that is what makes `--atif-img X up` one step.
    """
    named: list[tuple[str, str]] = []
    for adapter in registry.selected_adapters(settings):
        image = getattr(settings, f"{adapter.replace('-', '_')}_img", "") or ""
        if image:
            named.append((adapter, image))
    return named


def build_load_path_images(settings: Settings) -> None:
    """Build the images the load path deploys: the operators, and any adapter image this run names."""
    if settings.images_env_path.exists():
        info(f"removing {settings.images_env_path}: it records references this build replaces")
        settings.images_env_path.unlink()
    build_operators(settings)
    for adapter, image in loadable_adapter_images(settings):
        build_adapter(settings, adapter, image)


def load(settings: Settings) -> None:
    """Load the built images into the Kind cluster.

    `kind load docker-image` shells out to docker even on the podman provider, so export
    each image and hand Kind the archive. One archive per image: `podman save` collapses
    several into a single manifest entry and would load the first under every tag.

    The operator images come from the layout; an adapter image comes from the flag that
    named it, because the load path has no registry to hold it.

    How many images there are is the layout's answer, and the layout comes from the
    checkout. With neither a checkout nor a declared layout the paths below resolve
    against the current directory, so a module tree that happens to sit there would pick
    the modular layout for images built somewhere else; it is refused instead.
    """
    if not settings.operator_repo and not settings.operator_arch:
        raise HarnessError(
            "OPERATOR_REPO is not set and no layout is declared, so there is no telling which operator "
            "images this run built; pass --operator-repo, or --operator-arch single|modular when the "
            "checkout is not at hand"
        )
    built = images(settings)
    missing = [image for image in built if not image_present(settings, image)]
    if missing:
        raise HarnessError(f"{', '.join(missing)} is not in the engine's store; run 'k8s-tests images' first")
    adapters = loadable_adapter_images(settings)
    missing_adapters = [image for _, image in adapters if not image_present(settings, image)]
    if missing_adapters:
        raise HarnessError(
            f"{', '.join(missing_adapters)} is not in the engine's store; run "
            "'k8s-tests --adapters <name> images' first, or drop the adapter image flag"
        )
    built += [image for _, image in adapters]
    for label, image in named_images(settings):
        if image in built:
            continue
        if image_present(settings, image):
            built.append(image)
        else:
            warn(f"{label} image {image} is not in the engine's store; the cluster will pull it")

    kind_env = environment({"KIND_EXPERIMENTAL_PROVIDER": settings.kind_provider})
    with tempfile.TemporaryDirectory(prefix="k8s-tests-images-") as workdir:
        for image in built:
            archive = Path(workdir) / f"image-{image.replace('/', '-').replace(':', '-')}.tar"
            save(settings, image, archive)
            info(f"loading {image} into Kind cluster {settings.kind_cluster}")
            run([settings.kind, "load", "image-archive", "--name", settings.kind_cluster, archive], env=kind_env)
    info(f"loaded {len(built)} image(s) into {settings.kind_cluster}")


def require_cluster(settings: Settings) -> None:
    """Fail in seconds when the registry flow is asked for before the cluster exists."""
    from harness.cluster import clusters

    if settings.kind_cluster not in clusters(settings):
        raise HarnessError(
            f"Kind cluster {settings.kind_cluster} not found; create it with 'k8s-tests cluster' "
            "(or set KIND_PROVIDER to the engine that created it)"
        )


def require_checkout(path: str, name: str) -> Path:
    """A checkout the registry flow builds from, checked before anything is built."""
    if not path:
        raise HarnessError(f"{name} is not set; point it at a checkout, or place one beside this one")
    directory = Path(path).expanduser()
    if not directory.is_dir():
        raise HarnessError(f"{name}={directory} does not exist")
    return directory


def build_evalhub(settings: Settings, image: str) -> None:
    repo = require_checkout(settings.evalhub_repo, "EVALHUB_REPO")
    containerfile = repo / "Containerfile"
    if not containerfile.is_file():
        raise HarnessError(f"missing {containerfile}; set EVALHUB_REPO")
    args = [
        "--build-arg",
        "BUILD_NUMBER=k8s-tests",
        "--build-arg",
        f"BUILD_DATE={datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    ]
    if (repo / ".git").is_dir():
        revision = output(["git", "-C", repo, "rev-parse", "--short", "HEAD"], check=False)
        if revision:
            args += ["--build-arg", f"GIT_HASH={revision}"]
    build(settings, image, repo, containerfile, *args)


def build_adapter(settings: Settings, adapter: str, image: str) -> None:
    """Build one eval-hub-contrib adapter image, with the arguments its Containerfile takes."""
    repo = require_checkout(settings.evalhub_contrib_repo, "EVALHUB_CONTRIB_REPO")
    context = repo / registry.adapter_context(adapter)
    containerfile = context / "Containerfile"
    if not containerfile.is_file():
        raise HarnessError(f"missing {containerfile}; set EVALHUB_CONTRIB_REPO")
    if adapter == "toolbench-server":
        build(settings, image, context, containerfile, "--build-arg", "FETCH_FULL_CACHE=0")
    elif adapter == "promptfoo" and settings.base_image:
        build(settings, image, context, containerfile, "--build-arg", f"BASE_IMAGE={settings.base_image}")
    else:
        build(settings, image, context, containerfile)


def build_lmes(settings: Settings, image: str) -> None:
    """Build the lm_evaluation_harness job image from the fork's own Dockerfile.lmes-job."""
    repo = require_checkout(settings.lmes_repo, "LMES_REPO")
    containerfile = repo / LMES_DOCKERFILE
    if not containerfile.is_file():
        raise HarnessError(f"missing {containerfile}; set LMES_REPO")
    architecture = platform(settings).rpartition("/")[2]
    build(settings, image, repo, containerfile, "--build-arg", f"TARGETARCH={architecture}")


def push_and_maybe_remove(settings: Settings, image: str) -> None:
    """Push an image and, when asked, drop the local copy: the registry has it."""
    push(settings, image)
    if settings.remove_after_push:
        if run([container_engine(settings), "rmi", image], capture=True, check=False).returncode != 0:
            warn(f"could not remove {image} from the local store after pushing")


def images_all(settings: Settings) -> None:
    """Build every source image, push it to the local registry, and record the references."""
    require_cluster(settings)
    require_operator_repo(settings)
    if settings.skip_build and settings.remove_after_push:
        raise HarnessError(
            "SKIP_BUILD reuses the images in the local store, so it cannot be combined with REMOVE_AFTER_PUSH"
        )

    registry.ensure(settings)
    registry.configure_nodes(settings)

    recorded: dict[str, str] = {}
    built: list[str] = []

    workload = registry.registry_reference(settings, "trustyai-service-operator")
    module = registry.registry_reference(settings, "trustyai-operator-module")
    operator_names = [workload]
    if deploys_module_operator(settings):
        operator_names.append(module)
    if settings.skip_build:
        require_present(settings, tuple(operator_names))
    else:
        build_operators(settings.with_updates(workload_img=workload, module_img=module))
    for image in operator_names:
        push_and_maybe_remove(settings, image)
    built += operator_names
    recorded["WORKLOAD_IMG"] = workload
    if deploys_module_operator(settings):
        recorded["MODULE_IMG"] = module

    evalhub = registry.registry_reference(settings, "evalhub/evalhub")
    if settings.skip_build:
        require_present(settings, (evalhub,))
    else:
        build_evalhub(settings, evalhub)
    push_and_maybe_remove(settings, evalhub)
    built.append(evalhub)
    recorded["EVALHUB_IMG"] = evalhub

    for adapter in registry.selected_adapters(settings):
        image = registry.registry_reference(settings, f"evalhub/{registry.adapter_image_name(adapter)}")
        if settings.skip_build:
            require_present(settings, (image,))
        else:
            build_adapter(settings, adapter, image)
        push_and_maybe_remove(settings, image)
        built.append(image)
        match adapter:
            case "inspect":
                recorded["INSPECT_IMG"] = image
            case "lighteval":
                recorded["LIGHTEVAL_IMG"] = image
            case "atif":
                recorded["ATIF_IMG"] = image

    if registry.builds_lmes(settings):
        lmes = registry.registry_reference(settings, "evalhub/community-lmes")
        if settings.skip_build:
            require_present(settings, (lmes,))
        else:
            build_lmes(settings, lmes)
        push_and_maybe_remove(settings, lmes)
        built.append(lmes)
        recorded["LMES_IMG"] = lmes

    write_record(settings, recorded)
    info(f"pushed {len(built)} images to {settings.registry_host}")
    info(f"image references written to {settings.images_env_path}")
    info("deploy with those images: k8s-tests deploy-registry")


def require_present(settings: Settings, images_to_check: tuple[str, ...]) -> None:
    for image in images_to_check:
        if not image_present(settings, image):
            raise HarnessError(f"SKIP_BUILD is set but {image} is not present")


def write_record(settings: Settings, recorded: dict[str, str]) -> None:
    """Write what `images-all` pushed, in the order the checks read it."""
    path = settings.images_env_path
    path.parent.mkdir(parents=True, exist_ok=True)
    order = (
        "WORKLOAD_IMG",
        "MODULE_IMG",
        "EVALHUB_IMG",
        "LMES_IMG",
        "INSPECT_IMG",
        "LIGHTEVAL_IMG",
        "ATIF_IMG",
    )
    lines = [f"# Generated by k8s-tests images-all: images pushed to {settings.registry_host}"]
    lines += [f"{key}={recorded[key]}" for key in order if key in recorded]
    path.write_text("\n".join(lines) + "\n")


def clean(settings: Settings) -> None:
    """Remove the built images, the local registry and the HTML report, leaving the cluster."""
    if reachable_engine(settings) is None:
        warn("podman/docker is not reachable; leaving the built images and the local registry alone")
    else:
        for image in images(settings):
            remove(settings, image)
        remove_registry(settings)
    report_dir = settings.report_path.parent
    if report_dir.as_posix() not in ("", ".") and report_dir.is_dir():
        info(f"removing {report_dir}")
        shutil.rmtree(report_dir)


def remove_registry(settings: Settings) -> None:
    """Remove the registry container and the recorded images it and the local store hold.

    Removing the container drops the images the cluster pulled from it. Only the images
    the record names are dropped from the local store: one built and tagged there by hand
    is not this command's to delete.
    """
    engine = container_engine(settings)
    name = registry.registry_name(settings)
    if container_state(settings, name):
        info(f"removing local registry {name}")
        if run([engine, "container", "rm", "-f", name], capture=True, check=False).returncode != 0:
            warn(f"could not remove {name}")

    path = settings.images_env_path
    if not path.exists():
        return
    for line in record_lines(path):
        image = line.partition("=")[2]
        if image:
            remove(settings, image)
    info(f"removing {path}: every image it recorded is gone")
    path.unlink()


__all__ = [
    "LMES_DOCKERFILE",
    "MODULE_DIR",
    "build_adapter",
    "build_evalhub",
    "build_load_path_images",
    "build_lmes",
    "build_operators",
    "check_operator_repo",
    "clean",
    "images",
    "images_all",
    "load",
    "require_checkout",
    "require_cluster",
    "trees_differ",
    "warn_on_stale_template",
    "write_record",
]
