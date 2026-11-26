"""The local registry the Kind nodes pull locally built images from."""

from __future__ import annotations

import tempfile
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

from harness.cluster import node_containers
from harness.config import Settings
from harness.engine import container_engine, container_state, start_container
from harness.shell import HarnessError, info, output, run, succeeded, warn

REGISTRY_IMAGE = "docker.io/library/registry:2"
REGISTRY_INTERNAL_PORT = "5000"
KIND_NETWORK = "kind"

# Where containerd reads a per-registry hosts.toml from, and the config path that enables it.
CERTS_DIR = "/etc/containerd/certs.d"
REGISTRY_CONFIG_PATH_BLOCK = """
[plugins."io.containerd.grpc.v1.cri".registry]
  config_path = "/etc/containerd/certs.d"
"""

# The adapters eval-hub-contrib has. Listed rather than discovered so a typo in
# ADAPTERS fails instead of leaving that provider on the operator's own pin.
CONTRIB_ADAPTERS = (
    "atif",
    "clear",
    "databench",
    "deepeval",
    "evalhub-post-processor",
    "followbench",
    "guidellm",
    "ifbench",
    "inspect",
    "lighteval",
    "lm-eval",
    "mteb",
    "nemo-guardrails",
    "promptfoo",
    "ragas",
    "ruler",
    "swebench",
    "tooleval",
    "toolbench-server",
    "wildguard",
)

ADAPTER_IMAGE_NAMES = {"clear": "community-ibm-clear", "evalhub-post-processor": "evalhub-post-processor"}
ADAPTER_CONTEXTS = {"toolbench-server": "adapters/tooleval/tool-server"}


def registry_name(settings: Settings) -> str:
    return f"k8s-tests-registry-{settings.kind_cluster}"


def registry_alias(settings: Settings) -> str:
    """The name the nodes resolve the registry by: short, stable, derived from the cluster."""
    return f"k8s-tests-reg-{zlib.crc32(settings.kind_cluster.encode()) & 0xFFFFFFFF:08x}"


def registry_reference(settings: Settings, name: str) -> str:
    """An image reference under the local registry, as a pod has to spell it."""
    return f"{settings.registry_host}/{name}:{settings.tag}"


def adapter_image_name(adapter: str) -> str:
    return ADAPTER_IMAGE_NAMES.get(adapter, f"community-{adapter}")


def adapter_context(adapter: str) -> str:
    return ADAPTER_CONTEXTS.get(adapter, f"adapters/{adapter}")


def selected_adapters(settings: Settings) -> list[str]:
    """The adapters ADAPTERS asks for, checked against the ones that exist."""
    wanted = settings.adapters.replace(",", " ").split() or ["all"]
    # `none` builds no adapter at all, for a run whose checks need only the operators and
    # the image it was given; it stands alone, because naming it beside others is a contradiction.
    if "none" in wanted:
        if wanted != ["none"]:
            raise HarnessError("ADAPTERS=none stands alone; it cannot be combined with other adapters")
        return []
    for item in wanted:
        if item not in ("all", "lmes") and item not in CONTRIB_ADAPTERS:
            raise HarnessError(
                f"unknown ADAPTERS entry {item!r}; use all, lmes, or one of: {', '.join(CONTRIB_ADAPTERS)}"
            )
    if "all" in wanted:
        return list(CONTRIB_ADAPTERS)
    return [adapter for adapter in CONTRIB_ADAPTERS if adapter in wanted]


def builds_lmes(settings: Settings) -> bool:
    wanted = settings.adapters.replace(",", " ").split()
    return "all" in wanted or "lmes" in wanted


def wait_for_registry(settings: Settings, attempts: int = 30) -> None:
    url = f"http://127.0.0.1:{settings.registry_port}/v2/"
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2):  # noqa: S310 - loopback, plain HTTP by design
                return
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    raise HarnessError(f"local registry {registry_name(settings)} did not become reachable on {url}")


def ensure(settings: Settings) -> None:
    """Start (or reuse) the registry on the `kind` network, published on 127.0.0.1."""
    engine = container_engine(settings)
    name = registry_name(settings)

    if not succeeded([engine, "network", "inspect", KIND_NETWORK]):
        raise HarnessError(
            f"container network {KIND_NETWORK!r} does not exist for {engine}; create the cluster with "
            "'k8s-tests cluster', or set KIND_PROVIDER to the engine that created it"
        )

    alias = registry_alias(settings)
    state = container_state(settings, name)
    if state and state != "running":
        run([engine, "container", "start", name], capture=True)
    elif not state:
        create(settings, name, alias)

    published = output([engine, "port", name, f"{REGISTRY_INTERNAL_PORT}/tcp"], check=False).splitlines()
    expected = f"127.0.0.1:{settings.registry_port}"
    if not published or published[0].strip() != expected:
        raise HarnessError(
            f"registry {name} is published on {published[0].strip() if published else 'nothing'}, not {expected}; "
            "remove it or set REGISTRY_PORT"
        )

    if not attach(settings, name, alias):
        # A rootless container in pasta mode cannot take a second network, so one
        # created elsewhere has to go: the images it holds go with it.
        warn(f"registry {name} cannot join the {KIND_NETWORK} network; recreating it")
        run([engine, "container", "rm", "-f", name], capture=True)
        create(settings, name, alias)
    wait_for_registry(settings)
    info(f"local registry ready on {settings.registry_host}")


def create(settings: Settings, name: str, alias: str) -> None:
    """Start the registry container on the `kind` network, published on 127.0.0.1.

    It joins the network at creation: a rootless container created on the default
    network (pasta, on current podman) refuses `network connect` afterwards.
    """
    engine = container_engine(settings)
    if not succeeded([engine, "image", "inspect", REGISTRY_IMAGE]):
        run([engine, "image", "pull", REGISTRY_IMAGE])
    info(f"starting local registry {name} on 127.0.0.1:{settings.registry_port}")
    run(
        [
            engine,
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            KIND_NETWORK,
            "--network-alias",
            alias,
            "--label",
            f"io.k8s-tests.cluster={settings.kind_cluster}",
            "--publish",
            f"127.0.0.1:{settings.registry_port}:{REGISTRY_INTERNAL_PORT}",
            REGISTRY_IMAGE,
        ],
        capture=True,
    )


def attach(settings: Settings, name: str, alias: str) -> bool:
    """Join an existing registry container to the `kind` network under its alias.

    Returns False when the container cannot be attached - a rootless container in
    pasta mode takes no second network - and the caller recreates it.
    """
    engine = container_engine(settings)
    networks = output([engine, "container", "inspect", "--format", "{{json .NetworkSettings.Networks}}", name])
    if f'"{KIND_NETWORK}":' in networks and alias in networks:
        return True
    if f'"{KIND_NETWORK}":' in networks:
        run([engine, "network", "disconnect", KIND_NETWORK, name], check=False, capture=True)
    attached = (
        run([engine, "network", "connect", "--alias", alias, KIND_NETWORK, name], capture=True, check=False).returncode
        == 0
    )
    return attached and f'"{KIND_NETWORK}":' in output(
        [engine, "container", "inspect", "--format", "{{json .NetworkSettings.Networks}}", name]
    )


def hosts_config(settings: Settings) -> str:
    """The containerd mirror entry that makes `localhost:<port>` resolve to the registry."""
    return (
        f'[host."http://{registry_alias(settings)}:{REGISTRY_INTERNAL_PORT}"]\n  capabilities = ["pull", "resolve"]\n'
    )


def node_running(settings: Settings, node: str) -> None:
    """Make sure a node container is running and accepts exec, starting it when it is not."""
    engine = container_engine(settings)
    state = container_state(settings, node)
    if state == "running":
        return
    if not state:
        raise HarnessError(
            f"kind node {node} is not visible to {engine}; set KIND_PROVIDER to the engine that created the cluster"
        )
    info(f"starting stopped kind node {node}")
    start_container(settings, node)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if succeeded([engine, "exec", node, "true"]):
            return
        time.sleep(2)
    raise HarnessError(f"kind node {node} did not accept exec within 60s of starting")


def patch_containerd_enabled(settings: Settings) -> bool:
    """Whether a node without the certs.d config path may have it enabled in place."""
    return settings.patch_containerd.strip().lower() not in ("", "0", "false", "no")


def ensure_node_config(settings: Settings, node: str) -> None:
    """Give a node the certs.d config path, patching it in place when it was created without it."""
    engine = container_engine(settings)
    if succeeded([engine, "exec", node, "grep", "-q", "certs.d", "/etc/containerd/config.toml"]):
        return
    if not patch_containerd_enabled(settings):
        raise HarnessError(
            f"kind node {node} does not load {CERTS_DIR}; recreate the cluster ('k8s-tests down' then "
            "'k8s-tests cluster') so hack/kind/cluster.yaml's containerdConfigPatches takes effect"
        )
    warn(f"kind node {node} was created without the certs.d config path; enabling it in place")
    run(
        [
            engine,
            "exec",
            node,
            "sh",
            "-c",
            f"cat >>/etc/containerd/config.toml <<'EOF'\n{REGISTRY_CONFIG_PATH_BLOCK}EOF",
        ]
    )
    run([engine, "exec", node, "systemctl", "restart", "containerd"])


def configure_nodes(settings: Settings) -> None:
    """Write the mirror config on every node, so `localhost:<port>` resolves to the registry."""
    engine = container_engine(settings)
    nodes = node_containers(settings)
    if not nodes:
        raise HarnessError(
            f"Kind cluster {settings.kind_cluster} has no nodes visible to {engine}; "
            "set KIND_PROVIDER when the cluster runs on another engine"
        )
    directory = f"{CERTS_DIR}/localhost:{settings.registry_port}"
    with tempfile.TemporaryDirectory(prefix="k8s-tests-hosts-") as workdir:
        hosts = Path(workdir) / "hosts.toml"
        hosts.write_text(hosts_config(settings))
        hosts.chmod(0o644)
        for node in nodes:
            node_running(settings, node)
            ensure_node_config(settings, node)
            run([engine, "exec", node, "mkdir", "-p", directory])
            run([engine, "cp", hosts, f"{node}:{directory}/hosts.toml"], capture=True)
            info(f"configured localhost:{settings.registry_port} mirror on {node}")


__all__ = [
    "ADAPTER_CONTEXTS",
    "ADAPTER_IMAGE_NAMES",
    "CERTS_DIR",
    "CONTRIB_ADAPTERS",
    "KIND_NETWORK",
    "REGISTRY_IMAGE",
    "adapter_context",
    "adapter_image_name",
    "attach",
    "builds_lmes",
    "configure_nodes",
    "create",
    "ensure",
    "ensure_node_config",
    "hosts_config",
    "node_running",
    "patch_containerd_enabled",
    "registry_alias",
    "registry_name",
    "registry_reference",
    "selected_adapters",
    "wait_for_registry",
]
