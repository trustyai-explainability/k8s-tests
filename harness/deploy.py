"""Deploy the module operator, its module resource, and the workload operator under it.

The kustomize overlay is rendered from a temporary copy of the checkout's
`trustyai-operator-module/config`, so the operator image, the workload image it renders
and the target namespace can all be overridden without editing the checkout. The
operands on top - EvalHub instances, TrustyAIService, the jobs - are the tests' business.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml
from kubernetes.client.exceptions import ApiException

from harness import arch
from harness.cluster import kube, kube_result
from harness.config import (
    IMAGE_RECORD_VARS,
    IMAGES_ENV_DEPLOYED_MARKER,
    SETTINGS,
    Settings,
    is_deployed_record,
    record_lines,
    record_values,
)
from harness.engine import split_image
from harness.images import operator_repo
from harness.shell import HarnessError, info, warn
from utilities.certs import generate_serving_cert, service_dns_names

# Kind has no OpenShift Routes or Prometheus Operator, but the rendered manifests
# reference their types and the watchers need them to exist.
EXTERNAL_CRDS = (
    ("route_crd.yaml", "routes.route.openshift.io"),
    ("monitoring.coreos.com_servicemonitors.yaml", "servicemonitors.monitoring.coreos.com"),
)

# The KServe API. A 3.6 or earlier operator watches the kind, so the CRD has to be there
# before it starts; from 3.7 on nothing does, and a run that installs it anyway is not
# testing the install this harness is for. See `arch.SERVING_CRD_MAX_VERSION`.
SERVING_CRD = ("serving.kserve.io_inferenceservices.yaml", "inferenceservices.serving.kserve.io")

# Only the module operator seeds a Prometheus instance, and only a modular checkout ships
# the stand-in: a pre-module one has no prometheus API to satisfy.
MODULE_CRDS = (("monitoring.coreos.com_prometheuses.yaml", "prometheuses.monitoring.coreos.com"),)

# The certificates OpenShift's service CA would issue, and the Services their names cover.
TLS_SECRETS = ("webhook-server-cert", "trustyai-service-operator-metrics-tls")
CERT_SERVICES = ("trustyai-service-operator-webhook-service", "trustyai-service-operator-metrics-service")

MODULE_OPERATOR_DEPLOYMENT = "trustyai-operator-module-controller-manager"
MODULE_OPERATOR_IMAGE = "trustyai-operator-module-controller"
WORKLOAD_OPERATOR_DEPLOYMENT = "trustyai-service-operator-controller-manager"
# The ConfigMap a pre-module checkout's overlay generates, which is how its operator
# resolves its own image and the operand images.
OPERATOR_CONFIG_MAP = "trustyai-service-operator-config"

# Each RELATED_IMAGE_* variable is the module operator's own mapping of a workload
# params.env key onto the platform variable that fills it.
RELATED_IMAGES = (
    ("RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_PY_IMAGE", "trustyai_service_img", "TrustyAI service"),
    ("RELATED_IMAGE_ODH_EVAL_HUB_IMAGE", "evalhub_img", "EvalHub"),
    ("RELATED_IMAGE_ODH_KUBE_RBAC_PROXY_IMAGE", "kube_rbac_proxy_img", "kube-rbac-proxy"),
    ("RELATED_IMAGE_ODH_TA_LMES_JOB_IMAGE", "lmes_img", "lm_evaluation_harness adapter"),
    ("RELATED_IMAGE_ODH_EVAL_HUB_INSPECTAI_IMAGE", "inspect_img", "Inspect AI adapter"),
    ("RELATED_IMAGE_ODH_EVAL_HUB_LIGHTEVAL_IMAGE", "lighteval_img", "LightEval adapter"),
    ("RELATED_IMAGE_ODH_TRUSTYAI_GARAK_LLS_PROVIDER_DSP_IMAGE", "garak_img", "garak adapter"),
    ("RELATED_IMAGE_ODH_TRUSTYAI_NEMO_GUARDRAILS_SERVER_IMAGE", "nemo_img", "NeMo Guardrails server"),
)


def related_image_env(settings: Settings) -> list[tuple[str, str]]:
    """The RELATED_IMAGE_* variables the module operator hands to the workload operator."""
    variables = [("RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_OPERATOR_IMAGE", settings.workload_img)]
    for envvar, field, _ in RELATED_IMAGES:
        value = getattr(settings, field)
        if value:
            variables.append((envvar, value))
    return variables


def ensure_namespace(settings: Settings) -> None:
    info(f"creating namespace {settings.namespace}")
    manifest = kube(settings, "create", "namespace", settings.namespace, "--dry-run=client", "-o", "yaml", capture=True)
    kube(settings, "apply", "-f", "-", input_text=manifest)


def crd_entries(settings: Settings) -> list[tuple[str, str]]:
    """The CRD stand-ins this run applies, as (file, name) pairs in the order they are applied."""
    entries = list(EXTERNAL_CRDS)
    if arch.deploys_module_operator(settings):
        entries += list(MODULE_CRDS)
    if arch.requires_serving_crd(settings):
        entries.append(SERVING_CRD)
    return entries


def report_serving_crd(settings: Settings) -> None:
    """Say whether the serving API is installed, and warn when a run that needs nothing finds one.

    A cluster keeps a CRD across runs and this harness never deletes one, so a run that
    does not need serving can inherit it from an earlier 3.6 run. That run is not
    exercising the removal, and the checks that do skip, so it says so instead of passing
    quietly.
    """
    reason = arch.serving_crd_source(settings)
    if arch.requires_serving_crd(settings):
        info(f"installing the InferenceService CRD: the operators watch it ({reason})")
        return
    info(f"not installing the InferenceService CRD: nothing reads it ({reason})")
    if arch.deploys_module_operator(settings) and not settings.release:
        info(
            "nothing declared this run as 3.6; a 3.6 checkout needs the CRD, so name it "
            "(--release 3.6) or pass --serving-crd always"
        )
    if kube(settings, "get", "crd", SERVING_CRD[1], check=False, capture=True):
        warn(
            f"crd/{SERVING_CRD[1]} is already installed and this run leaves it alone; "
            "a cluster without it is what the isvc-free checks want"
        )


def apply_external_crds(settings: Settings) -> None:
    """Apply the CRDs Kind lacks, and wait for every one of them to serve.

    The wait has to cover the module CRDs too, not only the OpenShift stand-ins: the
    Prometheus the module operator's reconciliation expects is applied later, and a CR
    that reaches the API server before its CRD is Established is rejected.

    The InferenceService CRD is the one entry that follows the release rather than the API
    server: it is applied only for a run whose operators still watch the kind, which is
    what `crd_entries` and `report_serving_crd` decide.
    """
    directory = operator_repo(settings) / "tests" / "crds"
    if not directory.is_dir():
        raise HarnessError(f"missing {directory} (external CRD stand-ins)")
    report_serving_crd(settings)
    entries = crd_entries(settings)
    info(f"applying external CRDs from {directory}")
    for filename, _ in entries:
        path = directory / filename
        if not path.is_file():
            raise HarnessError(f"missing {path}")
        kube(settings, "apply", "-f", path, capture=True)
    for _, name in entries:
        kube(
            settings,
            "wait",
            "--for=condition=Established",
            f"crd/{name}",
            f"--timeout={settings.timeout}",
            capture=True,
        )


def ensure_tls_secrets(settings: Settings, workdir: Path) -> None:
    """Create the webhook and metrics certificates once, so repeat deploys keep the same one."""
    if kube(settings, "-n", settings.namespace, "get", "secret", "webhook-server-cert", check=False, capture=True):
        return
    info("creating the webhook and metrics TLS secrets")
    names = [
        name for service in CERT_SERVICES for name in service_dns_names(service=service, namespace=settings.namespace)
    ]
    certificate, key = generate_serving_cert(dns_names=names)
    certs = workdir / "certs"
    certs.mkdir(parents=True, exist_ok=True)
    (certs / "tls.crt").write_bytes(certificate)
    (certs / "tls.key").write_bytes(key)
    (certs / "tls.key").chmod(0o600)
    for secret in TLS_SECRETS:
        kube(
            settings,
            "-n",
            settings.namespace,
            "create",
            "secret",
            "tls",
            secret,
            f"--cert={certs / 'tls.crt'}",
            f"--key={certs / 'tls.key'}",
        )


def render_single_operator(settings: Settings, workdir: Path) -> Path:
    """Render the checkout's own overlay with this run's operator and operand images."""
    overlay = arch.single_operator_overlay(settings)
    relative = overlay.relative_to(operator_repo(settings))
    shutil.copytree(operator_repo(settings) / "config", workdir / "config")

    # The overlay turns the ConfigMap's values into the manager's image and its
    # RELATED_IMAGE_* environment through kustomize vars, so one patch moves the
    # Deployment and the operand images together.
    data = {"trustyaiOperatorImage": settings.workload_img, **operand_images(settings)}
    patch = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": OPERATOR_CONFIG_MAP},
        "data": data,
    }
    kustomization = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "namespace": settings.namespace,
        "resources": [f"./{relative.as_posix()}"],
        "patches": [
            {
                "target": {"version": "v1", "kind": "ConfigMap", "name": OPERATOR_CONFIG_MAP},
                "patch": yaml.safe_dump(patch, sort_keys=False),
            },
        ],
    }
    (workdir / "kustomization.yaml").write_text(yaml.safe_dump(kustomization, sort_keys=False))
    return workdir


def operand_images(settings: Settings) -> dict[str, str]:
    """The operand images a pre-module operator resolves from its ConfigMap.

    The keys are the params.env names this operator reads, which are also what the module
    operator maps the RELATED_IMAGE_* variables onto: `--lmes-img` therefore lands on
    `lmes-pod-image` here, the image an evaluation job's adapter runs.
    """
    images = {"trustyaiServiceImage": settings.trustyai_service_img} if settings.trustyai_service_img else {}
    if settings.evalhub_img:
        images["evalHubImage"] = settings.evalhub_img
    if settings.kube_rbac_proxy_img:
        images["kube-rbac-proxy"] = settings.kube_rbac_proxy_img
    if settings.lmes_img:
        images["lmes-pod-image"] = settings.lmes_img
    if settings.garak_img:
        images["garak-provider-image"] = settings.garak_img
    if settings.nemo_img:
        images["nemo-guardrails-image"] = settings.nemo_img
    return images


def render_module_operator(settings: Settings, workdir: Path) -> Path:
    """Write the kustomization that deploys the module operator with this run's images."""
    module_config = arch.module_tree(settings) / "config"
    if not (module_config / "default").is_dir():
        raise HarnessError(f"missing {module_config}/default")
    shutil.copytree(module_config, workdir / "config")

    image_name, image_tag = split_image(settings.module_img)
    operations = [
        {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": name, "value": value}}
        for name, value in related_image_env(settings)
    ]
    kustomization = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "namespace": settings.namespace,
        "resources": ["./config/default"],
        "images": [{"name": MODULE_OPERATOR_IMAGE, "newName": image_name, "newTag": image_tag}],
        "patches": [
            {
                "target": {"group": "apps", "version": "v1", "kind": "Deployment", "name": MODULE_OPERATOR_DEPLOYMENT},
                "patch": yaml.safe_dump(operations, sort_keys=False),
            }
        ],
    }
    (workdir / "kustomization.yaml").write_text(yaml.safe_dump(kustomization, sort_keys=False))
    return workdir


def seed_prometheus(settings: Settings) -> None:
    info("seeding the Prometheus instance the module operator expects")
    kube(
        settings,
        "apply",
        "-f",
        "-",
        input_text=yaml.safe_dump(
            {
                "apiVersion": "monitoring.coreos.com/v1",
                "kind": "Prometheus",
                "metadata": {"name": "e2e-prometheus", "namespace": settings.namespace},
            },
            sort_keys=False,
        ),
    )


def announce_operands(settings: Settings, audience: str) -> None:
    for _, field, label in RELATED_IMAGES:
        value = getattr(settings, field)
        if value:
            info(f"passing {label} image {value} to {audience}")


def point_conversion_webhooks(settings: Settings) -> None:
    """Point the CRD conversion webhooks at the CA this deploy seeded.

    OpenShift's service CA does this from the serving-cert annotation; on Kind the
    harness seeds the certificate, and until the webhooks trust it every read that
    converts between v1 and v1alpha1 fails. The modular layout gets it from
    `utilities.stack`, which also creates the module resource; the single layout has no
    such step, so it is done here.
    """
    from utilities import trustyai
    from utilities.kube import load_clients

    settings.export()
    core_client, _ = load_clients(context=settings.kube_context)
    bundle = trustyai.webhook_ca_bundle(core_client, settings.namespace)
    trustyai.point_conversion_webhooks_at_ca(core_client.api_client, bundle)
    info("pointed the TrustyAI and EvalHub conversion webhooks at the seeded CA")


def bring_up(settings: Settings) -> None:
    """Apply the module resource and wait for the workload operator it deploys."""
    from utilities import stack

    settings.export()
    info("applying the TrustyAI module resource and waiting for the workload operator")
    try:
        stack.bring_up()
    except (pytest.skip.Exception, pytest.fail.Exception) as exc:
        raise HarnessError(str(exc)) from exc
    except ApiException as exc:
        raise HarnessError(f"{exc.status} {exc.reason}: {(exc.body or '').strip()}") from exc


def deploy_modular(settings: Settings, workdir: Path) -> None:
    """Render the module operator from a copy of its config tree, with this run's images."""
    render_module_operator(settings, workdir)
    info(f"deploying the module operator ({settings.module_img}) with workload image {settings.workload_img}")
    announce_operands(settings, "the workload operator")
    kube(settings, "apply", "-k", workdir, "--server-side=true")
    # rollout status, not Available: an existing pod set satisfies Available while a new
    # image is still rolling out.
    info("waiting for the module operator deployment")
    kube(
        settings,
        "-n",
        settings.namespace,
        "rollout",
        "status",
        f"deployment/{MODULE_OPERATOR_DEPLOYMENT}",
        f"--timeout={settings.timeout}",
    )
    seed_prometheus(settings)
    ensure_tls_secrets(settings, workdir)


def _apply_output(applied: subprocess.CompletedProcess[str]) -> str:
    """What kubectl said about an apply, from either stream: a conflict goes to stderr."""
    return "\n".join(part for part in ((applied.stdout or "").strip(), (applied.stderr or "").strip()) if part)


def apply_overlay(settings: Settings, workdir: Path) -> None:
    """Apply a rendered tree server-side, taking ownership of fields another manager holds.

    A pre-module checkout ships an *aggregated* ClusterRole, whose `.rules` the
    clusterrole-aggregation-controller owns and rewrites from the aggregation labels. A
    re-deploy therefore conflicts on that field alone, and forcing it is what a second
    `deploy` has always needed; the first one applies cleanly and never gets here.

    The exit status decides what happened and the text only says why: a conflict is the
    one failure that can be recovered from, and it is reported on standard error, so both
    streams are read. Anything else stops the run rather than reading as a clean apply,
    which is what a failure with no conflict in its output otherwise looks like.
    """
    applied = kube_result(settings, "apply", "-k", workdir, "--server-side=true")
    output = _apply_output(applied)
    if applied.returncode == 0:
        return
    if "conflict" not in output.lower():
        raise HarnessError(f"applying {workdir} failed with exit status {applied.returncode}: {output[:400]}")
    ran = kube_result(settings, "apply", "-k", workdir, "--server-side=true", "--force-conflicts")
    retried = _apply_output(ran)
    if ran.returncode != 0:
        if "conflict" in retried.lower():
            raise HarnessError(f"deploying {workdir} conflicts with another manager: {retried[:400]}")
        raise HarnessError(
            f"re-applying {workdir} with --force-conflicts failed with exit status {ran.returncode}: {retried[:400]}"
        )
    warn("re-applied with --force-conflicts: an aggregated ClusterRole's rules belong to its aggregator")


def warn_on_moving_pins(settings: Settings) -> str:
    """What to say when a pre-module run leaves the EvalHub images at the operator's pins.

    A rhoai-3.5 checkout pins `evalhub:latest` and `ta-lmes-job:latest`: moving tags whose
    current content needs more RBAC than that operator grants (an EvalHub 1.0 service
    creates a NetworkPolicy per job) and expects a job contract it no longer speaks. The
    GA images of that release pair with it, so a run that wants EvalHub checks names them.
    """
    if settings.evalhub_img and settings.lmes_img:
        return ""
    missing = [
        flag
        for flag, value in (("--evalhub-img", settings.evalhub_img), ("--lmes-img", settings.lmes_img))
        if not value
    ]
    named = " and ".join(missing)
    return (
        f"{named} left at the operator's pin, which for a pre-module checkout is a moving "
        "`latest` tag: pass --evalhub-img quay.io/opendatahub/odh-eval-hub:odh-3.5 and "
        "--lmes-img quay.io/opendatahub/ta-lmes-job:odh-3.5-ga to run EvalHub jobs on that release"
    )


def deploy_single(settings: Settings, workdir: Path) -> None:
    """Render the pre-module checkout's own overlay, with this run's images."""
    # The operator mounts the webhook certificate, so the Secret has to exist before the
    # pod is scheduled; the modular layout has the module operator come up first instead.
    ensure_tls_secrets(settings, workdir)
    overlay = render_single_operator(settings, workdir)
    relative = arch.single_operator_overlay(settings).relative_to(operator_repo(settings))
    info(f"deploying {relative.as_posix()} ({settings.workload_img})")
    announce_operands(settings, "the operator")
    apply_overlay(settings, overlay)
    info("waiting for the operator deployment")
    kube(
        settings,
        "-n",
        settings.namespace,
        "rollout",
        "status",
        f"deployment/{WORKLOAD_OPERATOR_DEPLOYMENT}",
        f"--timeout={settings.timeout}",
    )


def deploy(settings: Settings, *, next_step: bool = True) -> None:
    """Deploy the layout the checkout has, and wait for the operator it runs."""
    arch.require_operator_repo(settings)
    kept = keep_used_images(settings)
    layout = arch.operator_arch(settings)

    with tempfile.TemporaryDirectory(prefix="k8s-tests-deploy-") as workdir:
        path = Path(workdir)
        ensure_namespace(settings)
        apply_external_crds(settings)
        if layout == arch.ARCH_SINGLE:
            deploy_single(settings, path)
        else:
            deploy_modular(settings, path)

    if layout == arch.ARCH_SINGLE:
        hint = warn_on_moving_pins(settings)
        if hint:
            warn(hint)
        point_conversion_webhooks(settings)
    else:
        # The module resource, the workload operator it deploys and the webhook CA all
        # come from the stack bring-up.
        bring_up(settings)

    stamp_record(settings, kept if kept is not None else deployed_record(settings))
    info(f"TrustyAI is deployed in namespace {settings.namespace} ({layout} layout)")
    if next_step:
        info("its operands are created by the tests: k8s-tests test")


def deployed_record(settings: Settings) -> list[str]:
    """The images this deployment runs, as record lines, for a run that built loads instead of pushes.

    `images-all` writes the record while it pushes; the load path has no registry, so the
    record is written from the resolved settings here instead. Either way the file describes
    what is running, which is what lets `test` pick up an adapter image without being told
    it a second time.
    """
    by_envvar = {setting.envvar: setting.field for setting in SETTINGS}
    lines: list[str] = []
    for variable in IMAGE_RECORD_VARS:
        field = by_envvar.get(variable)
        value = getattr(settings, field, "") if field else ""
        if value:
            lines.append(f"{variable}={value}")
    return lines


def keep_used_images(settings: Settings) -> list[str] | None:
    """Drop the recorded images this deployment does not use, and say what is left.

    A build that was pushed but never deployed, or a deploy that failed half way, must not
    leave a file the checks treat as a record of what is running: the record stays
    unstamped until `stamp_record` runs at the end of a successful deploy.
    """
    path = settings.images_env_path
    if not path.exists():
        return None
    kept: list[str] = []
    dropped: list[str] = []
    for line in record_lines(path):
        key, _, value = line.partition("=")
        if value and value != getattr(settings, key.lower(), None):
            dropped.append(line)
        else:
            kept.append(line)
    if dropped:
        warn(f"{path} records images this deploy does not use ({' '.join(dropped)}); dropping them")
    if not kept:
        warn(f"removing {path}: it records no image this deploy uses")
        path.unlink()
        return None
    return kept


def stamp_record(settings: Settings, kept: list[str] | None) -> None:
    """Mark the record as describing this deployment, now that the stack runs it."""
    if kept is None:
        return
    path = settings.images_env_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([f"# {IMAGES_ENV_DEPLOYED_MARKER}", *kept]) + "\n")
    info(f"recorded the deployed images in {path}")


def registry_deploy(settings: Settings) -> None:
    """Deploy the images `images-all` pushed, reading the references it recorded."""
    path = settings.images_env_path
    if not path.exists():
        raise HarnessError(f"{path} is missing; run 'k8s-tests images-all' first")
    recorded = record_values(path)
    if is_deployed_record(path):
        info(f"{path} already describes a deployment; reusing its references")
    deploy(settings.with_image_record(recorded))


def registry_up(settings: Settings) -> None:
    """Create the cluster, build and push every image, then deploy them."""
    from harness import cluster, images

    cluster.create(settings)
    images.images_all(settings)
    registry_deploy(settings)


__all__ = [
    "EXTERNAL_CRDS",
    "MODULE_CRDS",
    "MODULE_OPERATOR_DEPLOYMENT",
    "OPERATOR_CONFIG_MAP",
    "SERVING_CRD",
    "WORKLOAD_OPERATOR_DEPLOYMENT",
    "RELATED_IMAGES",
    "TLS_SECRETS",
    "apply_external_crds",
    "apply_overlay",
    "announce_operands",
    "bring_up",
    "crd_entries",
    "deploy",
    "deploy_modular",
    "deploy_single",
    "ensure_namespace",
    "ensure_tls_secrets",
    "keep_used_images",
    "registry_deploy",
    "registry_up",
    "operand_images",
    "point_conversion_webhooks",
    "report_serving_crd",
    "warn_on_moving_pins",
    "related_image_env",
    "render_module_operator",
    "render_single_operator",
    "seed_prometheus",
    "stamp_record",
]
