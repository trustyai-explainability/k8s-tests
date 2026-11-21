"""The Kind cluster: create it, wake it up, delete it, and run kubectl against it."""

from __future__ import annotations

import subprocess
import time

from harness import resources
from harness.config import Settings
from harness.engine import cluster_owner_engine, container_engine, container_state, start_container
from harness.shell import HarnessError, environment, info, output, require_tool, run, warn

NODE_LABEL = "io.x-k8s.kind.cluster"

# How long a just-started node gets before the API server has to answer.
DEFAULT_API_TIMEOUT = 300


def kind_environment(settings: Settings, provider: str = "") -> dict[str, str]:
    """The environment Kind runs in, with the provider the cluster was created with.

    Without it Kind asks its default (docker) provider and reports a podman cluster as
    missing.
    """
    chosen = provider or settings.kind_provider or cluster_owner_engine(settings)
    return environment({"KIND_EXPERIMENTAL_PROVIDER": chosen})


def kube(
    settings: Settings,
    *args: object,
    check: bool = True,
    capture: bool = False,
    input_text: str | None = None,
) -> str:
    """Run kubectl pinned to the Kind context, returning its standard output."""
    result = run(
        [settings.kubectl, "--context", settings.kube_context, *args],
        check=check,
        capture=capture,
        input_text=input_text,
    )
    return (result.stdout or "").strip()


def kube_result(
    settings: Settings,
    *args: object,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run kubectl pinned to the Kind context and hand back the completed process.

    Both streams are captured and a failure is not raised, so a caller that has to read
    the exit status as well as the output - an apply that may be recoverable - can. A
    server-side apply reports its field conflicts on standard error.
    """
    return run(
        [settings.kubectl, "--context", settings.kube_context, *args],
        check=False,
        capture=True,
        input_text=input_text,
    )


def clusters(settings: Settings) -> list[str]:
    """The Kind clusters this host has, when Kind can list them."""
    result = run([settings.kind, "get", "clusters"], capture=True, check=False, env=kind_environment(settings))
    if result.returncode != 0:
        return []
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def node_containers(settings: Settings) -> list[str]:
    """The containers that are this cluster's nodes, which only the engine can list."""
    listed = output(
        [
            container_engine(settings),
            "ps",
            "-a",
            "--filter",
            f"label={NODE_LABEL}={settings.kind_cluster}",
            "--format",
            "{{.Names}}",
        ]
    )
    return [line.strip() for line in listed.splitlines() if line.strip()]


def start_nodes(settings: Settings) -> None:
    """Start the node containers, which a podman machine restart leaves stopped."""
    for node in node_containers(settings):
        if container_state(settings, node) == "running":
            continue
        info(f"starting Kind node {node}")
        start_container(settings, node)


def start_registry(settings: Settings) -> None:
    """Start the local registry container when a previous run left it stopped."""
    from harness.registry import registry_name

    name = registry_name(settings)
    state = container_state(settings, name)
    if state and state != "running":
        info(f"starting local registry {name}")
        start_container(settings, name)


def timeout_seconds(timeout: str) -> int:
    """A kubectl duration - `300s`, `5m`, a bare number - as seconds."""
    value = timeout.strip()
    if value.isdigit():
        return int(value)
    for suffix, scale in (("s", 1), ("m", 60), ("h", 3600)):
        if value.endswith(suffix) and value[: -len(suffix)].isdigit():
            return int(value[: -len(suffix)]) * scale
    return DEFAULT_API_TIMEOUT


def wait_for_api(settings: Settings) -> None:
    """Wait for the API server, which a just-started node needs a moment to answer."""
    deadline = time.monotonic() + timeout_seconds(settings.timeout)
    while time.monotonic() < deadline:
        if (
            run(
                [settings.kubectl, "--context", settings.kube_context, "get", "--raw=/readyz"],
                capture=True,
                check=False,
            ).returncode
            == 0
        ):
            return
        time.sleep(2)
    raise HarnessError(f"the API server for {settings.kube_context} did not become reachable within {settings.timeout}")


def create(settings: Settings) -> None:
    """Create the Kind cluster (idempotent), wake its nodes up, and wait for them."""
    require_tool(settings.kind, "install kind, or set KIND to its path")
    require_tool(settings.kubectl, "install kubectl, or set KUBECTL to its path")

    if settings.kind_cluster in clusters(settings):
        info(f"Kind cluster {settings.kind_cluster} already exists")
    else:
        config = resources.resolve(settings.kind_config, resources.kind_config())
        if not config.is_file():
            raise HarnessError(f"no Kind configuration at {config}; set KIND_CONFIG to one")
        command: list[object] = [
            settings.kind,
            "create",
            "cluster",
            "--name",
            settings.kind_cluster,
            "--config",
            config,
        ]
        if settings.kind_node_image:
            command += ["--image", settings.kind_node_image]
        info(f"creating Kind cluster {settings.kind_cluster}")
        run([*command, "--wait", "5m"], env=kind_environment(settings))

    start_nodes(settings)
    start_registry(settings)
    wait_for_api(settings)
    kube(settings, "wait", "--for=condition=Ready", "nodes", "--all", f"--timeout={settings.timeout}")
    info(f"Kind cluster {settings.kind_cluster} is ready on context {settings.kube_context}")


def delete(settings: Settings) -> None:
    require_tool(settings.kind, "install kind, or set KIND to its path")
    info(f"deleting Kind cluster {settings.kind_cluster}")
    run([settings.kind, "delete", "cluster", "--name", settings.kind_cluster], env=kind_environment(settings))


def logs(settings: Settings) -> None:
    """Tail the module and workload operator logs."""
    warn("tailing the operator logs; Ctrl-C stops the tail")
    run(
        [
            settings.kubectl,
            "--context",
            settings.kube_context,
            "-n",
            settings.namespace,
            "logs",
            "-l",
            "control-plane=controller-manager",
            "--tail=100",
            "-f",
        ]
    )


def status(settings: Settings) -> None:
    """What is running: the module CR, the operators, EvalHub, the emulator and their pods."""
    emulator_namespace = settings.emulator_namespace or settings.namespace
    reads = (
        ("get", "trustyais.components.platform.opendatahub.io"),
        ("-n", settings.namespace, "get", "deployment,pods", "-l", "app.kubernetes.io/name=trustyai-operator-module"),
        ("-n", settings.namespace, "get", "deployment,pods", "-l", "control-plane=trustyai-service-operator"),
        ("get", "evalhubs.trustyai.opendatahub.io", "-A"),
        ("get", "deploy,pods", "-A", "-l", "app=eval-hub"),
        ("-n", emulator_namespace, "get", "deployment,service,pods", "-l", "app=vllm-emulator"),
    )
    for read in reads:
        printed = kube(settings, *read, "--ignore-not-found", check=False)
        if printed:
            print(printed)


__all__ = [
    "DEFAULT_API_TIMEOUT",
    "NODE_LABEL",
    "clusters",
    "create",
    "delete",
    "kind_environment",
    "kube",
    "kube_result",
    "logs",
    "node_containers",
    "start_nodes",
    "start_registry",
    "status",
    "timeout_seconds",
    "wait_for_api",
]
