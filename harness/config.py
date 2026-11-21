"""Harness settings: one table of flags, environment variables and defaults.

The table is what `k8s-tests --help` prints, the variables it reads, and what
`Settings.export` publishes back for the checks, which read the environment directly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

# What identifies a checkout, as opposed to an installed copy of the package.
REPOSITORY_MARKERS = ("pyproject.toml", "tests", "hack")
ROOT_VAR = "K8S_TESTS_ROOT"


def is_checkout(directory: Path) -> bool:
    return all((directory / marker).exists() for marker in REPOSITORY_MARKERS)


def repository_root(start: Path | None = None) -> Path | None:
    """The checkout this harness belongs to, or None when it was installed without one."""
    named = os.environ.get(ROOT_VAR, "").strip()
    if named:
        candidate = Path(named).expanduser().resolve()
        return candidate if is_checkout(candidate) else None
    location = (start or Path(__file__)).resolve()
    for directory in (location, *location.parents):
        if is_checkout(directory):
            return directory
    return None


REPO_ROOT = repository_root() or Path(__file__).resolve().parents[1]

# The operator's ODH overlay pins the retired Java service, which crash-loops; this is
# the Python rewrite the operator is written for.
DEFAULT_TRUSTYAI_SERVICE_IMG = "quay.io/trustyai/trustyai-service:latest"

DEFAULT_IMAGE_TAG = "dev"
DEFAULT_REGISTRY_PORT = "5005"
DEFAULT_IMAGES_ENV_FILE = "reports/images.env"

KEEP_RESOURCES_VAR = "KEEP_RESOURCES"

# The images `images-all` records and `deploy-registry` reads back.
IMAGE_RECORD_VARS = (
    "WORKLOAD_IMG",
    "MODULE_IMG",
    "EVALHUB_IMG",
    "LMES_IMG",
    "INSPECT_IMG",
    "LIGHTEVAL_IMG",
)

# Header `deploy` writes once the stack running the recorded images is up;
# `utilities.trustyai` only trusts a file carrying it.
IMAGES_ENV_DEPLOYED_MARKER = "Written by k8s-tests deploy"


@dataclass(frozen=True)
class Setting:
    field: str
    envvar: str
    default: str
    help: str
    flag: str = ""
    kind: Literal["str", "choice", "flag", "dir", "file"] = "str"
    choices: tuple[str, ...] = ()

    @property
    def option(self) -> str:
        return self.flag or "--" + self.field.replace("_", "-")


SETTINGS: tuple[Setting, ...] = (
    Setting(
        field="operator_repo",
        envvar="OPERATOR_REPO",
        default="",
        kind="dir",
        help="trustyai-service-operator checkout the images and manifests come from (required by images, deploy)",
    ),
    Setting(
        field="operator_arch",
        envvar="OPERATOR_ARCH",
        default="",
        kind="choice",
        choices=("modular", "single"),
        help=(
            "operator layout to deploy: modular (a 3.6+ checkout, module operator) or single (3.5 and "
            "earlier); empty follows the checkout, and naming one the checkout lacks is an error"
        ),
    ),
    Setting(
        field="release",
        envvar="OPERATOR_RELEASE",
        flag="--release",
        default="",
        help=(
            "RHOAI/ODH release the operator checkout is from, for example 3.5 or 3.6; the checks "
            "declaring a different range are skipped (OPERATOR_RELEASE, so it cannot be confused "
            "with the release of something else in the environment)"
        ),
    ),
    Setting(
        field="namespace",
        envvar="NAMESPACE",
        default="system",
        help="namespace the operators and their operands are deployed into",
    ),
    Setting(
        field="kube_context", envvar="KUBE_CONTEXT", default="", help="kubeconfig context; empty uses kind-<cluster>"
    ),
    Setting(field="kubectl", envvar="KUBECTL", default="kubectl", help="kubectl to shell out to"),
    Setting(field="kind", envvar="KIND", default="kind", help="kind binary to shell out to"),
    Setting(field="kind_cluster", envvar="KIND_CLUSTER", default="trustyai", help="Kind cluster name"),
    Setting(
        field="kind_config",
        envvar="KIND_CONFIG",
        default="",
        kind="file",
        help="Kind cluster configuration; empty uses the one the harness ships",
    ),
    Setting(
        field="kind_node_image",
        envvar="KIND_NODE_IMAGE",
        default="",
        help="Kind node image; empty uses the one Kind ships with",
    ),
    Setting(
        field="kind_provider",
        envvar="KIND_PROVIDER",
        default="",
        kind="choice",
        choices=("docker", "podman"),
        help="Kind provider to force on a host that has both; empty uses the engine that owns the cluster",
    ),
    Setting(
        field="engine",
        envvar="ENGINE",
        default="",
        kind="choice",
        choices=("docker", "podman"),
        help="container engine to build with; empty uses podman when it is reachable, otherwise docker",
    ),
    Setting(field="tag", envvar="TAG", default=DEFAULT_IMAGE_TAG, help="tag given to the images this harness builds"),
    Setting(
        field="image_prefix",
        envvar="IMAGE_PREFIX",
        default="localhost",
        help="registry prefix the load-path images are named under",
    ),
    Setting(
        field="workload_img",
        envvar="WORKLOAD_IMG",
        default="",
        help="workload (service) operator image; empty uses <prefix>/trustyai-service-operator:<tag>",
    ),
    Setting(
        field="module_img",
        envvar="MODULE_IMG",
        default="",
        help="module operator image; empty uses <prefix>/trustyai-operator-module:<tag>",
    ),
    Setting(
        field="evalhub_img",
        envvar="EVALHUB_IMG",
        default="",
        help="EvalHub operand image; empty keeps the operator's own pin",
    ),
    Setting(
        field="trustyai_service_img",
        envvar="TRUSTYAI_SERVICE_IMG",
        default=DEFAULT_TRUSTYAI_SERVICE_IMG,
        help="TrustyAI service operand image handed to the workload operator",
    ),
    Setting(
        field="kube_rbac_proxy_img",
        envvar="KUBE_RBAC_PROXY_IMG",
        default="",
        help="kube-rbac-proxy sidecar image; empty keeps the operator's pin (published for amd64 only)",
    ),
    Setting(
        field="lmes_img",
        envvar="LMES_IMG",
        default="",
        help="lm_evaluation_harness adapter image; empty keeps the operator's pin",
    ),
    Setting(
        field="inspect_img",
        envvar="INSPECT_IMG",
        default="",
        help="Inspect AI adapter image; empty keeps the operator's pin",
    ),
    Setting(
        field="lighteval_img",
        envvar="LIGHTEVAL_IMG",
        default="",
        help="LightEval adapter image; empty keeps the operator's pin",
    ),
    Setting(field="timeout", envvar="TIMEOUT", default="300s", help="how long each kubectl wait and rollout may take"),
    Setting(field="emulator_img", envvar="EMULATOR_IMG", default="", help="LLM emulator image the fixtures deploy"),
    Setting(
        field="emulator_namespace",
        envvar="EMULATOR_NAMESPACE",
        default="",
        help="namespace the fixtures deploy the LLM emulator into; empty uses the harness namespace",
    ),
    Setting(field="emulator_token", envvar="EMULATOR_TOKEN", default="", help="token the emulator requires"),
    Setting(
        field="model_url",
        envvar="MODEL_URL",
        default="",
        help="OpenAI-compatible model endpoint the job checks call instead of the emulator",
    ),
    Setting(
        field="model_key",
        envvar="MODEL_KEY",
        default="",
        help="api key for MODEL_URL; prefer the variable, a flag lands in the process list",
    ),
    Setting(field="model_name", envvar="MODEL_NAME", default="", help="model name a job submits"),
    Setting(
        field="report_file", envvar="REPORT_FILE", default="reports/report.html", help="HTML report the checks write"
    ),
    Setting(
        field="adapters_report_file",
        envvar="ADAPTERS_REPORT_FILE",
        default="reports/adapters.html",
        help="HTML report the adapter section writes, so it does not overwrite the other one",
    ),
    Setting(
        field="adapters_marker",
        envvar="ADAPTERS_MARKER",
        default="adapters",
        help="marker that selects the adapter section of the checks",
    ),
    Setting(
        field="pytest_args",
        envvar="PYTEST_ARGS",
        default="",
        help="extra pytest arguments, shell-split, appended to every run",
    ),
    Setting(
        field="registry_port",
        envvar="REGISTRY_PORT",
        default=DEFAULT_REGISTRY_PORT,
        help="port the local registry images-all starts, and the nodes pull from, listens on",
    ),
    Setting(
        field="evalhub_repo",
        envvar="EVALHUB_REPO",
        default="",
        kind="dir",
        help="eval-hub checkout images-all builds the EvalHub image from; empty uses a sibling of this checkout",
    ),
    Setting(
        field="evalhub_contrib_repo",
        envvar="EVALHUB_CONTRIB_REPO",
        default="",
        kind="dir",
        help="eval-hub-contrib checkout images-all builds the adapter images from; empty uses a sibling",
    ),
    Setting(
        field="lmes_repo",
        envvar="LMES_REPO",
        default="",
        kind="dir",
        help="lm-evaluation-harness checkout images-all builds the job image from; empty uses a sibling",
    ),
    Setting(
        field="adapters",
        envvar="ADAPTERS",
        default="all",
        help="adapters images-all builds: all, lmes, or a comma-separated list of eval-hub-contrib directories",
    ),
    Setting(
        field="platform",
        envvar="PLATFORM",
        default="",
        help="platform the images are built for; empty builds for the engine's own (linux/arm64 on Apple Silicon)",
    ),
    Setting(
        field="images_env_file",
        envvar="IMAGES_ENV_FILE",
        default=DEFAULT_IMAGES_ENV_FILE,
        kind="file",
        help="file images-all records the pushed references in, and deploy reads back",
    ),
    Setting(
        field="base_image",
        envvar="BASE_IMAGE",
        default="",
        help="base image for the promptfoo adapter build; empty uses the adapter's own",
    ),
    Setting(
        field="skip_build",
        envvar="SKIP_BUILD",
        default="",
        kind="flag",
        help="images-all pushes the images already in the engine's store instead of building them",
    ),
    Setting(
        field="remove_after_push",
        envvar="REMOVE_AFTER_PUSH",
        default="",
        kind="flag",
        help="images-all drops each image from the engine's store once it is pushed",
    ),
    Setting(
        field="patch_containerd",
        envvar="PATCH_CONTAINERD",
        default="1",
        help="images-all enables the certs.d registry config on a node created without it instead of refusing",
    ),
)


@dataclass(frozen=True)
class Settings:
    """The resolved settings, with the derived values filled in."""

    operator_repo: str = ""
    operator_arch: str = ""
    release: str = ""
    namespace: str = "system"
    kube_context: str = ""
    kubectl: str = "kubectl"
    kind: str = "kind"
    kind_cluster: str = "trustyai"
    kind_config: str = ""
    kind_node_image: str = ""
    kind_provider: str = ""
    engine: str = ""
    tag: str = DEFAULT_IMAGE_TAG
    image_prefix: str = "localhost"
    workload_img: str = ""
    module_img: str = ""
    evalhub_img: str = ""
    trustyai_service_img: str = DEFAULT_TRUSTYAI_SERVICE_IMG
    kube_rbac_proxy_img: str = ""
    lmes_img: str = ""
    inspect_img: str = ""
    lighteval_img: str = ""
    timeout: str = "300s"
    emulator_img: str = ""
    emulator_namespace: str = ""
    emulator_token: str = ""
    model_url: str = ""
    model_key: str = ""
    model_name: str = ""
    report_file: str = "reports/report.html"
    adapters_report_file: str = "reports/adapters.html"
    adapters_marker: str = "adapters"
    pytest_args: str = ""
    registry_port: str = DEFAULT_REGISTRY_PORT
    evalhub_repo: str = ""
    evalhub_contrib_repo: str = ""
    lmes_repo: str = ""
    adapters: str = "all"
    platform: str = ""
    images_env_file: str = DEFAULT_IMAGES_ENV_FILE
    base_image: str = ""
    skip_build: bool = False
    remove_after_push: bool = False
    patch_containerd: str = "1"
    keep_resources: bool = False
    #: The settings a flag or variable actually named, so the image record only fills gaps.
    explicit: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.kube_context:
            object.__setattr__(self, "kube_context", f"kind-{self.kind_cluster}")
        if not self.workload_img:
            object.__setattr__(self, "workload_img", self.default_workload_img)
        if not self.module_img:
            object.__setattr__(self, "module_img", self.default_module_img)
        if not self.evalhub_repo:
            object.__setattr__(self, "evalhub_repo", self.sibling_checkout("eval-hub"))
        if not self.evalhub_contrib_repo:
            object.__setattr__(self, "evalhub_contrib_repo", self.sibling_checkout("eval-hub-contrib"))
        if not self.lmes_repo:
            object.__setattr__(self, "lmes_repo", self.sibling_checkout("lm-evaluation-harness"))

    def sibling_checkout(self, name: str) -> str:
        """A checkout beside this one, which is where the defaults expect one."""
        root = repository_root()
        return str((root or REPO_ROOT).parent / name) if root is not None else ""

    @property
    def default_workload_img(self) -> str:
        return f"{self.image_prefix}/trustyai-service-operator:{self.tag}"

    @property
    def default_module_img(self) -> str:
        return f"{self.image_prefix}/trustyai-operator-module:{self.tag}"

    @property
    def registry_host(self) -> str:
        return f"localhost:{self.registry_port}"

    @property
    def images_env_path(self) -> Path:
        """The image record: absolute, relative to the checkout, else relative to the caller."""
        path = Path(self.images_env_file).expanduser()
        if path.is_absolute():
            return path
        root = repository_root() or Path.cwd()
        return root / path

    @property
    def report_path(self) -> Path:
        """The HTML report, resolved against the directory the command was run in."""
        return Path(self.report_file).expanduser()

    @property
    def adapters_report_path(self) -> Path:
        return Path(self.adapters_report_file).expanduser()

    def with_updates(self, **updates: Any) -> Settings:
        """These settings with some values replaced; the values other ones derive from follow them."""
        derived = {
            "workload_img": self.workload_img if self.workload_img != self.default_workload_img else "",
            "module_img": self.module_img if self.module_img != self.default_module_img else "",
        }
        # The context follows the cluster name unless the caller named one - including when
        # the cluster is named on a command rather than on the group (`cluster --kind-cluster
        # other`), where the group has already derived a context from its own default.
        if "kind_cluster" in updates and "kube_context" not in updates and "kube_context" not in self.explicit:
            derived["kube_context"] = ""
        named = frozenset(name for name, value in updates.items() if value is not None)
        merged = {**derived, **{name: value for name, value in updates.items() if value is not None}}
        return replace(self, **merged, explicit=self.explicit | named)

    def with_image_record(self, values: Mapping[str, str]) -> Settings:
        """Fill in the images `images-all` recorded, keeping the ones the caller named."""
        by_envvar = {setting.envvar: setting.field for setting in SETTINGS}
        updates = {
            by_envvar[key]: value
            for key, value in values.items()
            if key in IMAGE_RECORD_VARS and value and key not in self.explicit_envvars
        }
        return self.with_updates(**updates) if updates else self

    @property
    def explicit_envvars(self) -> frozenset[str]:
        return frozenset(setting.envvar for setting in SETTINGS if setting.field in self.explicit)

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> Settings:
        """Build the settings from click's parameters, defaulting the ones it did not set."""
        values: dict[str, Any] = {}
        explicit: set[str] = set()
        for setting in SETTINGS:
            given = params.get(setting.field)
            if given is not None:
                explicit.add(setting.field)
            if setting.kind == "flag":
                values[setting.field] = bool(given)
            else:
                values[setting.field] = setting.default if given is None else given
        values["keep_resources"] = bool(params.get("keep_resources", False))
        return cls(**values, explicit=frozenset(explicit))

    def export(self) -> None:
        """Publish the resolved values where the checks read them."""
        for setting in SETTINGS:
            value = getattr(self, setting.field)
            if setting.kind == "flag":
                os.environ[setting.envvar] = "1" if value else ""
            elif setting.field == "images_env_file":
                # Absolute, so a run from another directory reads the same record.
                os.environ[setting.envvar] = str(self.images_env_path)
            else:
                os.environ[setting.envvar] = str(value)
        os.environ[KEEP_RESOURCES_VAR] = "1" if self.keep_resources else ""


def record_lines(path: Path) -> list[str]:
    """The `KEY=value` lines of an image record, comments and blanks dropped."""
    try:
        text = path.read_text()
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def record_values(path: Path) -> dict[str, str]:
    """The image references an `images-all` run wrote, keyed by variable name."""
    values: dict[str, str] = {}
    for line in record_lines(path):
        key, _, value = line.partition("=")
        if value:
            values[key] = value
    return values


def is_deployed_record(path: Path) -> bool:
    """Whether an image record describes a deployment rather than a build."""
    try:
        text = path.read_text()
    except OSError:
        return False
    return any(line.lstrip().startswith("#") and IMAGES_ENV_DEPLOYED_MARKER in line for line in text.splitlines())


__all__ = [
    "DEFAULT_IMAGES_ENV_FILE",
    "DEFAULT_IMAGE_TAG",
    "DEFAULT_REGISTRY_PORT",
    "DEFAULT_TRUSTYAI_SERVICE_IMG",
    "IMAGE_RECORD_VARS",
    "IMAGES_ENV_DEPLOYED_MARKER",
    "KEEP_RESOURCES_VAR",
    "REPOSITORY_MARKERS",
    "REPO_ROOT",
    "ROOT_VAR",
    "SETTINGS",
    "Setting",
    "Settings",
    "is_checkout",
    "is_deployed_record",
    "record_lines",
    "record_values",
    "repository_root",
]
