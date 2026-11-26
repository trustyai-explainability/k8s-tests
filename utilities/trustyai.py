"""Helpers for the TrustyAI module and workload operator checks.

The checks cover the modular stack: the module operator, the workload operator it
deploys, and the images both run. They stay client-only on purpose: the Kind
harness has no platform operator, no OLM and no CSV, so a check that would need
one of those skips with a reason instead of being faked.

Deployments are read through the Apps API, ConfigMaps through the Core API and
the TrustyAI custom resource through the dynamic client.
"""

from __future__ import annotations

import logging
import os
import pathlib
import time
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import ConflictError, NotFoundError, ResourceNotFoundError

LOGGER = logging.getLogger(__name__)

MODULE_API_GROUP = "components.platform.opendatahub.io"
MODULE_KIND = "TrustyAI"
MODULE_CRD_NAME = "trustyais.components.platform.opendatahub.io"
MODULE_CR_NAME = "default-trustyai"
MODULE_OPERATOR_DEPLOYMENT = "trustyai-operator-module-controller-manager"
WORKLOAD_OPERATOR_DEPLOYMENT = "trustyai-service-operator-controller-manager"

# Image variables `k8s-tests images-all` records in reports/images.env. The file lets
# a test run compare the deployment against the images that were pushed without
# asking the caller to pass every variable again.
IMAGE_ENV_VARS = (
    "WORKLOAD_IMG",
    "MODULE_IMG",
    "EVALHUB_IMG",
    "NEMO_IMG",
    "LMES_IMG",
    "INSPECT_IMG",
    "LIGHTEVAL_IMG",
    "ATIF_IMG",
)
# The two load-path images the harness computes from IMAGE_PREFIX and TAG, so a
# value that is only that default can be told apart from one the caller chose.
LOAD_PATH_IMAGES = {"WORKLOAD_IMG": "trustyai-service-operator", "MODULE_IMG": "trustyai-operator-module"}
IMAGE_PREFIX_VAR = "IMAGE_PREFIX"
IMAGE_TAG_VAR = "TAG"
# `k8s-tests deploy` rewrites reports/images.env with this header when the deployment
# ran the values it records; before that the file only lists what was pushed.
IMAGES_ENV_DEPLOYED_MARKER = "Written by k8s-tests deploy"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
IMAGES_ENV_FILE = REPO_ROOT / "reports" / "images.env"


def image_env_file() -> pathlib.Path:
    """The file holding the image references: IMAGES_ENV_FILE when it is set.

    The harness commands that write, deploy and clean read the same variable, so a
    run that moved the file is read back from where it wrote it. A relative value is
    resolved against the checkout.
    """
    override = os.environ.get("IMAGES_ENV_FILE")
    if not override:
        return IMAGES_ENV_FILE
    path = pathlib.Path(override)
    return path if path.is_absolute() else REPO_ROOT / path


def load_path_default(key: str) -> str:
    """The image the harness computes for `key` when the caller did not set it."""
    prefix = os.environ.get(IMAGE_PREFIX_VAR) or "localhost"
    tag = os.environ.get(IMAGE_TAG_VAR) or "dev"
    return f"{prefix}/{LOAD_PATH_IMAGES[key]}:{tag}"


def load_image_env(path: pathlib.Path | None = None) -> None:
    """Fill image variables from the file a registry deploy recorded.

    `k8s-tests images-all` writes what it pushed; `k8s-tests deploy` keeps the keys
    it deployed and marks the file with IMAGES_ENV_DEPLOYED_MARKER. Only a marked
    file is read, so images that were built but never deployed cannot make the
    checks compare against something the cluster does not run.

    The harness exports `WORKLOAD_IMG`/`MODULE_IMG` with their load-path defaults,
    so a value that is only that default is treated as unset. Anything else - a
    value passed as a flag or variable, or a different prefix - wins.
    """
    try:
        lines = (path or image_env_file()).read_text().splitlines()
    except OSError:
        return
    if not any(line.startswith("#") and IMAGES_ENV_DEPLOYED_MARKER in line for line in lines):
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key not in IMAGE_ENV_VARS or not value:
            continue
        current = os.environ.get(key)
        if not current or (key in LOAD_PATH_IMAGES and current == load_path_default(key)):
            os.environ[key] = value


load_image_env()

# The module resource the fixture creates. Its name is fixed by a CEL rule on the
# CRD, so it is not a parameter anywhere in the harness.
MODULE_MANAGEMENT_STATE = "Managed"
# The services the harness enables. The module operator turns these into the
# workload operator's --enable-services argument, and the controllers all live in
# that one manager process: this is the only place a service is switched on, and
# there is no per-test alternative because the module resource is a cluster-scoped
# singleton. All four are enabled so any check can create its own operand in its own
# namespace - a TrustyAIService, an EvalHub instance, an LMEvalJob - the way
# opendatahub-tests does, rather than because a check exists for each today.
# An empty block would enable all four anyway; naming them keeps the resource
# explicit about what it turns on.
MODULE_ENABLED_SERVICES: dict[str, bool] = {
    "tas": True,
    "lmes": True,
    "evalHub": True,
    "nemoGuardrails": True,
}

# Bringing the stack up from a resource that did not exist yet includes a
# reconcile, a rollout and an image pull, so these waits are longer than the
# default ones.
STACK_TIMEOUT = 600
STACK_INTERVAL = 5

# CRDs the workload operator serves conversion webhooks for, and the secret
# holding the CA the harness seeds for them.
CONVERSION_CRDS = ("trustyaiservices.trustyai.opendatahub.io", "evalhubs.trustyai.opendatahub.io")
WEBHOOK_CERT_SECRET = "webhook-server-cert"
RELATED_IMAGE_PREFIX = "RELATED_IMAGE_"

# Env var through which the module operator tells itself which workload operator
# image to render. `k8s-tests deploy` patches it in.
WORKLOAD_IMAGE_RELATED_ENV = "RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_OPERATOR_IMAGE"
# Image variables the harness exports for the images it built.
MODULE_IMAGE_VAR = "MODULE_IMG"
WORKLOAD_IMAGE_VAR = "WORKLOAD_IMG"

# ConfigMap written by the TrustyAI operator itself.
WORKLOAD_CONFIG_MAP = "trustyai-service-operator-config"
WORKLOAD_IMAGE_CONFIG_KEY = "trustyaiOperatorImage"

CONDITION_READY = "Ready"
CONDITION_PROVISIONING_SUCCEEDED = "ProvisioningSucceeded"

DEFAULT_WAIT_TIMEOUT = 300
WAIT_INTERVAL = 5


def crd_exists(api_client: client.ApiClient, name: str) -> bool:
    """Whether the named custom resource definition is installed."""
    try:
        client.ApiextensionsV1Api(api_client).read_custom_resource_definition(name=name)
    except ApiException as exc:
        if exc.status != 404:
            raise
        LOGGER.info("CRD %s is not installed", name)
        return False
    LOGGER.info("CRD %s is installed", name)
    return True


def deployment_exists(apps_client: client.AppsV1Api, name: str, namespace: str) -> bool:
    """Whether the named Deployment exists, without failing the run when it does not."""
    try:
        apps_client.read_namespaced_deployment(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        LOGGER.info("deployment %s/%s is not present", namespace, name)
        return False
    return True


def module_api_version(api_client: client.ApiClient) -> str:
    """The storage version of the TrustyAI module CRD, so the client never guesses."""
    crd = client.ApiextensionsV1Api(api_client).read_custom_resource_definition(name=MODULE_CRD_NAME)
    versions = crd.spec.versions or []
    storage = [version.name for version in versions if version.storage]
    if storage:
        return storage[0]
    if not versions:
        pytest.fail(f"CRD {MODULE_CRD_NAME} declares no versions", pytrace=False)
    return versions[0].name


def _module_resource(dynamic_client: DynamicClient, api_version: str) -> Any:
    try:
        return dynamic_client.resources.get(api_version=f"{MODULE_API_GROUP}/{api_version}", kind=MODULE_KIND)
    except ResourceNotFoundError:
        pytest.fail(
            f"{MODULE_API_GROUP}/{api_version} {MODULE_KIND} is not served on this cluster; "
            "run 'k8s-tests up' or 'k8s-tests deploy' first",
            pytrace=False,
        )


def module_manifest(
    api_version: str,
    name: str = MODULE_CR_NAME,
    enabled_services: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """The module resource, built here rather than read from a manifest file."""
    return {
        "apiVersion": f"{MODULE_API_GROUP}/{api_version}",
        "kind": MODULE_KIND,
        "metadata": {"name": name},
        "spec": {
            "managementState": MODULE_MANAGEMENT_STATE,
            "enabledServices": dict(MODULE_ENABLED_SERVICES if enabled_services is None else enabled_services),
        },
    }


def module_exists(dynamic_client: DynamicClient, api_version: str) -> bool:
    """Whether the cluster already has the singleton module resource."""
    resource = _module_resource(dynamic_client, api_version)
    try:
        resource.get(name=MODULE_CR_NAME)
    except NotFoundError:
        LOGGER.info("%s/%s is not present", MODULE_KIND, MODULE_CR_NAME)
        return False
    LOGGER.info("%s/%s is present", MODULE_KIND, MODULE_CR_NAME)
    return True


def ensure_module(
    dynamic_client: DynamicClient,
    api_version: str,
    enabled_services: dict[str, bool] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create the module resource unless the cluster has one; return it and who owns it.

    A resource that already exists is read back and left untouched: on a
    platform-managed install it belongs to the platform operator, and rewriting its
    spec would be the harness deciding how the platform runs TrustyAI. The second
    element of the result says whether this session created it, which is what
    decides whether the session may delete it again.
    """
    resource = _module_resource(dynamic_client, api_version)
    if module_exists(dynamic_client, api_version):
        return resource.get(name=MODULE_CR_NAME).to_dict(), False
    body = module_manifest(api_version, enabled_services=enabled_services)
    try:
        created = resource.create(body=body).to_dict()
    except ConflictError:
        LOGGER.info("%s/%s was created by someone else; leaving its spec alone", MODULE_KIND, MODULE_CR_NAME)
        return resource.get(name=MODULE_CR_NAME).to_dict(), False
    enabled = body["spec"]["enabledServices"]
    LOGGER.info(
        "created %s/%s with %s",
        MODULE_KIND,
        MODULE_CR_NAME,
        ", ".join(service for service, on in enabled.items() if on) or "no service",
    )
    return created, True


def apply_module(
    dynamic_client: DynamicClient,
    api_version: str,
    enabled_services: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Create the module resource, or converge an existing one on the spec this harness needs.

    `k8s-tests deploy` runs this through `utilities.stack`, so re-deploying resets a
    resource somebody edited by hand - the services the harness needs are the ones
    it declares. The fixture does not apply it: it adopts what it finds (see
    `ensure_module`), because a platform-managed resource is not the harness's to
    rewrite.
    """
    resource = _module_resource(dynamic_client, api_version)
    body = module_manifest(api_version, enabled_services=enabled_services)
    if module_exists(dynamic_client, api_version):
        converged = resource.patch(
            name=MODULE_CR_NAME,
            body={"spec": body["spec"]},
            content_type="application/merge-patch+json",
        ).to_dict()
        LOGGER.info("converged %s/%s on the services this harness needs", MODULE_KIND, MODULE_CR_NAME)
        return converged
    created = resource.create(body=body).to_dict()
    LOGGER.info("created %s/%s", MODULE_KIND, MODULE_CR_NAME)
    return created


def delete_module(dynamic_client: DynamicClient, api_version: str, timeout: int = DEFAULT_WAIT_TIMEOUT) -> None:
    """Delete the module resource this session created and wait for it to go."""
    resource = _module_resource(dynamic_client, api_version)
    LOGGER.info("deleting %s/%s", MODULE_KIND, MODULE_CR_NAME)
    try:
        resource.delete(name=MODULE_CR_NAME)
    except NotFoundError:
        LOGGER.info("%s/%s is already gone", MODULE_KIND, MODULE_CR_NAME)
        return
    deadline = time.monotonic() + timeout
    while True:
        try:
            resource.get(name=MODULE_CR_NAME)
        except NotFoundError:
            LOGGER.info("%s/%s is gone", MODULE_KIND, MODULE_CR_NAME)
            return
        if time.monotonic() >= deadline:
            LOGGER.warning("%s/%s is still present after %ss", MODULE_KIND, MODULE_CR_NAME, timeout)
            return
        time.sleep(WAIT_INTERVAL)


def read_module(dynamic_client: DynamicClient, api_version: str) -> dict[str, Any]:
    """Return the singleton TrustyAI module resource as a dict."""
    resource = _module_resource(dynamic_client, api_version)
    LOGGER.info("reading %s/%s", MODULE_API_GROUP, MODULE_CR_NAME)
    try:
        module = resource.get(name=MODULE_CR_NAME).to_dict()
    except NotFoundError:
        pytest.fail(
            f"{MODULE_KIND}/{MODULE_CR_NAME} was not found; the module operator has not reconciled "
            "its custom resource on this cluster",
            pytrace=False,
        )
    return module


def module_conditions(module: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map the module resource's status conditions by type."""
    status = module.get("status") or {}
    return {condition.get("type"): condition for condition in status.get("conditions") or []}


def module_condition(module: dict[str, Any], condition_type: str) -> str | None:
    """Return the status string of a module condition, if the condition is reported."""
    condition = module_conditions(module).get(condition_type)
    return condition.get("status") if condition else None


# How much of a condition's message one line of a failure message keeps. The module's
# `Ready` aggregates every enabled service's operand instances, so the one that is not
# ready is named in that message and nowhere else.
MAX_CONDITION_MESSAGE = 120


def _oneline(text: str | None, limit: int = MAX_CONDITION_MESSAGE) -> str:
    """A message held to one line and `limit` characters, so a summary stays a summary."""
    collapsed = " ".join((text or "").split())
    if not collapsed:
        return "no message"
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def describe_module_conditions(module: dict[str, Any]) -> str:
    """Readable one-line summary of every module condition, and what each one says.

    The message is included because the reason alone does not say which operand instance
    made the module `Not Ready`: `Ready=False (ServicesNotReady)` names no subject, and
    `Ready=False (ServicesNotReady: TAS: ns/name: Not all service components are ready)`
    says exactly which one to look at.
    """
    conditions = module_conditions(module)
    if not conditions:
        return "none"
    return ", ".join(
        f"{condition_type}={condition.get('status')} ({condition.get('reason')}: {_oneline(condition.get('message'))})"
        for condition_type, condition in sorted(conditions.items())
    )


def wait_for_module_condition(
    dynamic_client: DynamicClient,
    api_version: str,
    condition_type: str,
    status: str = "True",
    timeout: int = DEFAULT_WAIT_TIMEOUT,
) -> dict[str, Any]:
    """Poll the module resource until `condition_type` reports `status`."""
    deadline = time.monotonic() + timeout
    observed: dict[str, Any] = {}
    while True:
        observed = read_module(dynamic_client=dynamic_client, api_version=api_version)
        if module_condition(observed, condition_type) == status:
            LOGGER.info("%s/%s %s=%s", MODULE_KIND, MODULE_CR_NAME, condition_type, status)
            return observed
        if time.monotonic() >= deadline:
            break
        LOGGER.info(
            "waiting for %s=%s on %s/%s; conditions: %s",
            condition_type,
            status,
            MODULE_KIND,
            MODULE_CR_NAME,
            describe_module_conditions(observed),
        )
        time.sleep(WAIT_INTERVAL)
    pytest.fail(
        f"{MODULE_KIND}/{MODULE_CR_NAME} did not report {condition_type}={status} within {timeout}s; "
        f"conditions: {describe_module_conditions(observed)}",
        pytrace=False,
    )


def assert_module_ready(
    dynamic_client: DynamicClient,
    api_version: str,
    timeout: int = DEFAULT_WAIT_TIMEOUT,
) -> dict[str, Any]:
    """Wait until the module resource reports Ready=True and return it."""
    return wait_for_module_condition(
        dynamic_client=dynamic_client,
        api_version=api_version,
        condition_type=CONDITION_READY,
        status="True",
        timeout=timeout,
    )


def assert_module_provisioning_succeeded(module: dict[str, Any]) -> None:
    """Assert the module operator finished applying the workload manifests."""
    status = module_condition(module, CONDITION_PROVISIONING_SUCCEEDED)
    assert status == "True", (
        f"expected {CONDITION_PROVISIONING_SUCCEEDED}=True on {MODULE_KIND}/{MODULE_CR_NAME}, got {status!r}; "
        f"conditions: {describe_module_conditions(module)}"
    )


def assert_module_observed_generation(module: dict[str, Any]) -> None:
    """Assert status.observedGeneration tracks the current spec generation."""
    status = module.get("status") or {}
    observed_generation = status.get("observedGeneration")
    generation = (module.get("metadata") or {}).get("generation")
    LOGGER.info("%s/%s generation: %s observed %s", MODULE_KIND, MODULE_CR_NAME, generation, observed_generation)
    assert observed_generation == generation, (
        f"expected observedGeneration={generation} on {MODULE_KIND}/{MODULE_CR_NAME}, got {observed_generation}"
    )


def read_config_map(
    core_client: client.CoreV1Api,
    name: str,
    namespace: str,
) -> client.V1ConfigMap | None:
    """Return the named ConfigMap, or None when the cluster does not have it."""
    try:
        config_map = core_client.read_namespaced_config_map(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        LOGGER.info("ConfigMap %s/%s is not present", namespace, name)
        return None
    LOGGER.info("ConfigMap %s/%s is present with %d key(s)", namespace, name, len(config_map.data or {}))
    return config_map


def deployment_env(apps_client: client.AppsV1Api, name: str, namespace: str, env_name: str) -> str | None:
    """Value of an env var on the Deployment's first container, or None when unset."""
    deployment = apps_client.read_namespaced_deployment(name=name, namespace=namespace)
    containers = deployment.spec.template.spec.containers or []
    if not containers:
        return None
    for env in containers[0].env or []:
        if env.name == env_name:
            LOGGER.info("%s/%s container %s: %s=%s", namespace, name, containers[0].name, env_name, env.value)
            return env.value
    LOGGER.info("%s/%s container %s does not set %s", namespace, name, containers[0].name, env_name)
    return None


def expected_image_from_env(var_name: str) -> str | None:
    """Image the harness built and loaded, when the run came through `k8s-tests test`."""
    image = os.environ.get(var_name) or ""
    return image or None


# --- bringing the stack up -----------------------------------------------
#
# The module resource is built here rather than shipped as a manifest, so the
# waits that follow applying it live here too: the workload operator Deployment
# appearing, the values the module operator renders it with, and the conversion
# webhooks the checks go through. `utilities.stack` runs them for
# `k8s-tests deploy`, and the `trustyai_module` fixture runs them for a cluster
# that was brought up another way.


def _read_deployment_or_none(
    apps_client: client.AppsV1Api,
    name: str,
    namespace: str,
) -> client.V1Deployment | None:
    """The named Deployment, or None while it does not exist yet."""
    try:
        return apps_client.read_namespaced_deployment(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
    return None


def deployment_env_map(apps_client: client.AppsV1Api, name: str, namespace: str) -> dict[str, str]:
    """Every env var with a plain value on the Deployment's first container, by name."""
    deployment = _read_deployment_or_none(apps_client, name, namespace)
    if deployment is None:
        return {}
    containers = deployment.spec.template.spec.containers or []
    if not containers:
        return {}
    return {entry.name: entry.value for entry in containers[0].env or [] if entry.value is not None}


def wait_for_workload_operator(
    apps_client: client.AppsV1Api,
    namespace: str,
    timeout: int = STACK_TIMEOUT,
) -> client.V1Deployment:
    """Poll until the module operator has deployed the workload operator."""
    LOGGER.info("waiting for the module operator to deploy %s/%s", namespace, WORKLOAD_OPERATOR_DEPLOYMENT)
    deadline = time.monotonic() + timeout
    while True:
        deployment = _read_deployment_or_none(apps_client, WORKLOAD_OPERATOR_DEPLOYMENT, namespace)
        if deployment is not None:
            LOGGER.info("%s/%s exists", namespace, WORKLOAD_OPERATOR_DEPLOYMENT)
            return deployment
        if time.monotonic() >= deadline:
            break
        time.sleep(STACK_INTERVAL)
    pytest.fail(
        f"the module operator did not create deployment {namespace}/{WORKLOAD_OPERATOR_DEPLOYMENT} within {timeout}s; "
        f"check the {MODULE_OPERATOR_DEPLOYMENT} logs and the {MODULE_KIND}/{MODULE_CR_NAME} conditions",
        pytrace=False,
    )


def _rendered_problems(deployment: client.V1Deployment | None, wanted: dict[str, str]) -> list[str]:
    """How the workload operator Deployment differs from the values the module operator was told."""
    if deployment is None:
        return ["the workload operator Deployment is absent"]
    containers = deployment.spec.template.spec.containers or []
    if not containers:
        return ["the pod template runs no containers"]
    problems: list[str] = []
    workload_image = wanted.get(WORKLOAD_IMAGE_RELATED_ENV)
    if workload_image and containers[0].image != workload_image:
        problems.append(f"image is {containers[0].image}, expected {workload_image}")
    resolved = {entry.name: entry.value for entry in containers[0].env or []}
    for name, value in wanted.items():
        if name == WORKLOAD_IMAGE_RELATED_ENV:
            continue
        if resolved.get(name) != value:
            problems.append(f"{name} is {resolved.get(name)!r}, expected {value!r}")
    return problems


def wait_for_rendered_workload(
    apps_client: client.AppsV1Api,
    namespace: str,
    timeout: int = STACK_TIMEOUT,
) -> client.V1Deployment | None:
    """Poll until the workload operator runs and resolves what the module operator was told.

    The module operator renders the workload operator from the params.env its image
    carries, and rewrites that file from its own environment on reconcile. A
    workload operator that exists but still runs the previous image, or still
    resolves an operand image this run did not ask for, would pin the operands the
    checks create to the wrong build.

    Only a module operator that renders the workload image from a RELATED_IMAGE_*
    value is checked: that is how the harness overrides it, and a module operator
    deployed another way (a platform operator's params.env) has no such value to
    compare against, so there is nothing to wait for.
    """
    wanted = {
        name: value
        for name, value in deployment_env_map(apps_client, MODULE_OPERATOR_DEPLOYMENT, namespace).items()
        if name.startswith(RELATED_IMAGE_PREFIX)
    }
    if WORKLOAD_IMAGE_RELATED_ENV not in wanted:
        LOGGER.info(
            "%s/%s does not render the workload image from a %s value; not waiting for rendered values",
            namespace,
            MODULE_OPERATOR_DEPLOYMENT,
            WORKLOAD_IMAGE_RELATED_ENV,
        )
        return _read_deployment_or_none(apps_client, WORKLOAD_OPERATOR_DEPLOYMENT, namespace)
    LOGGER.info("%s/%s was told to render %s", namespace, MODULE_OPERATOR_DEPLOYMENT, wanted)
    deadline = time.monotonic() + timeout
    problems: list[str] = []
    while True:
        deployment = _read_deployment_or_none(apps_client, WORKLOAD_OPERATOR_DEPLOYMENT, namespace)
        problems = _rendered_problems(deployment, wanted)
        if not problems:
            LOGGER.info("%s/%s renders the requested values", namespace, WORKLOAD_OPERATOR_DEPLOYMENT)
            return deployment
        if time.monotonic() >= deadline:
            break
        LOGGER.info(
            "waiting for %s/%s to render the requested values: %s",
            namespace,
            WORKLOAD_OPERATOR_DEPLOYMENT,
            "; ".join(problems),
        )
        time.sleep(STACK_INTERVAL)
    pytest.fail(
        f"{namespace}/{WORKLOAD_OPERATOR_DEPLOYMENT} did not render the requested values within {timeout}s: "
        f"{'; '.join(problems)}",
        pytrace=False,
    )


def webhook_ca_bundle(core_client: client.CoreV1Api, namespace: str) -> str | None:
    """Base64 CA of the webhook certificate the harness seeds, if it seeded one.

    OpenShift's service CA writes the conversion webhooks' caBundle itself and
    nothing creates this secret there; on Kind the harness creates it before the
    operators start, because the operator pods do not come up without it.
    """
    try:
        secret = core_client.read_namespaced_secret(WEBHOOK_CERT_SECRET, namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        LOGGER.info(
            "Secret %s/%s is not present; leaving the conversion webhook CA to the platform",
            namespace,
            WEBHOOK_CERT_SECRET,
        )
        return None
    bundle = (secret.data or {}).get("tls.crt")
    LOGGER.info("read the webhook CA bundle from %s/%s", namespace, WEBHOOK_CERT_SECRET)
    return bundle


def point_conversion_webhooks_at_ca(api_client: client.ApiClient, ca_bundle: str | None) -> None:
    """Point the conversion webhooks of the CRDs the checks convert through at the CA."""
    if not ca_bundle:
        return
    apiextensions = client.ApiextensionsV1Api(api_client)
    for name in CONVERSION_CRDS:
        try:
            crd = apiextensions.read_custom_resource_definition(name=name)
        except ApiException as exc:
            if exc.status != 404:
                raise
            LOGGER.info("CRD %s is not installed; nothing to point at the webhook CA", name)
            continue
        conversion = crd.spec.conversion
        if conversion is None or conversion.strategy != "Webhook":
            LOGGER.info("CRD %s does not convert through a webhook; leaving it alone", name)
            continue
        apiextensions.patch_custom_resource_definition(
            name=name,
            body={"spec": {"conversion": {"webhook": {"clientConfig": {"caBundle": ca_bundle}}}}},
            _content_type="application/merge-patch+json",
        )
        LOGGER.info("patched the %s conversion webhook CA bundle", name)
