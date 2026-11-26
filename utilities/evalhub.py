"""Helpers for the EvalHub checks.

The tests create the EvalHub instance, its tenant namespace and, because Kind has
no service CA, its TLS material; the operator owns everything below that. The API
checks call EvalHub through a `kubectl port-forward` to its Service, since the
harness has neither an Ingress nor an OpenShift Route.

The kube-rbac-proxy sidecar serves the TLS endpoint with the certificate the
tests generate, whose subject alternative names are the in-cluster Service names.
A forwarded port is reached as 127.0.0.1, so the API checks do not verify the
certificate; they are not a TLS test.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import time
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import NotFoundError

from utilities.certs import generate_serving_cert, service_dns_names
from utilities.kube import keep_resources
from utilities.portforward import port_forward as _forward_port

LOGGER = logging.getLogger(__name__)

EVALHUB_API_GROUP = "trustyai.opendatahub.io"
EVALHUB_KIND = "EvalHub"
EVALHUB_PLURAL = "evalhubs"
EVALHUB_CRD_NAME = f"{EVALHUB_PLURAL}.{EVALHUB_API_GROUP}"
EVALHUB_NAME = "evalhub"
EVALHUB_SERVICE_PORT = 8443
EVALHUB_HEALTH_PATH = "/api/v1/health"
EVALHUB_HEALTH_STATUS_HEALTHY = "healthy"
EVALHUB_CONTAINER_NAME = "evalhub"
EVALHUB_PROXY_CONTAINER_NAME = "kube-rbac-proxy"
EVALHUB_APP_LABEL = "eval-hub"
EVALHUB_COMPONENT_LABEL = "api"
EVALHUB_DATABASE_BACKEND = "sqlite"
EVALHUB_IMAGE_VAR = "EVALHUB_IMG"
EVALHUB_IMAGE_RELATED_ENV = "RELATED_IMAGE_ODH_EVAL_HUB_IMAGE"
# A namespace carrying this label is a tenant of an EvalHub instance: the
# operator seeds its discovery, service CA and MLflow CA ConfigMaps there.
EVALHUB_TENANT_LABEL_KEY = "evalhub.trustyai.opendatahub.io/tenant"
EVALHUB_TENANT_LABEL_VALUE = "true"
# ConfigMaps the operator writes into a tenant namespace.
EVALHUB_TENANT_CONFIG_MAPS = (
    f"{EVALHUB_NAME}-service-ca",
    f"{EVALHUB_NAME}-mlflow-ca-bundle",
    "evalhub-discovery",
)
EVALHUB_JOBS_PATH = "/api/v1/evaluations/jobs"
EVALHUB_PROVIDERS_PATH = "/api/v1/evaluations/providers"
# Terminal job states; anything else keeps polling.
JOB_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
# kube-rbac-proxy authorizes the API with namespace-scoped SubjectAccessReviews
# whose namespace comes from the X-Tenant header, against these pseudo-resources.
TENANT_API_RESOURCES = ("evaluations", "providers", "collections", "status-events")
TENANT_API_VERBS = ["get", "list", "create", "update", "patch", "delete"]
TENANT_IDENTITY_NAME = "evalhub-api"
# The rules the operator writes into a tenant namespace for the tenant's own users
# (controllers/evalhub/tenant_roles.go tenantUserRules): the API's pseudo-resources,
# with collection reads only. The collection generation API is authorized as
# `evaluations`, so this is the grant a generation request is checked against, and
# the reason a request that fell through to the generic collections entry - a
# DELETE, or a POST - would be refused.
TENANT_USER_RULES: dict[str, list[str]] = {
    "evalhubs": ["get", "list"],
    "evaluations": ["get", "list", "create", "update", "patch", "delete"],
    "collections": ["get", "list", "watch"],
    "providers": ["get", "list", "watch"],
    "status-events": ["create"],
}
# The identity the authorization checks act as: the operator's tenant-user grant,
# so a path the policy maps to a resource or verb that grant lacks is refused.
TENANT_USER_IDENTITY_NAME = "evalhub-tenant-user"
# Names the operator does not manage: see grant_tenant_job_access.
TENANT_JOB_ACCESS_BINDING = "k8s-tests-instance-api-job-access"
TENANT_JOB_ACCESS_ROLES = (
    "trustyai-service-operator-evalhub-job-config",
    "trustyai-service-operator-evalhub-jobs-writer",
)
TENANT_RBAC_TIMEOUT = 120

CONDITION_READY = "Ready"
CONDITION_MONITORING_DEGRADED = "MonitoringDegraded"

DEFAULT_WAIT_TIMEOUT = 300
WAIT_INTERVAL = 5


def evalhub_api_version(api_client: client.ApiClient) -> str:
    """Storage version of the EvalHub CRD, so the client never guesses."""
    crd = client.ApiextensionsV1Api(api_client).read_custom_resource_definition(name=EVALHUB_CRD_NAME)
    versions = crd.spec.versions or []
    storage = [version.name for version in versions if version.storage]
    if storage:
        return storage[0]
    if not versions:
        pytest.fail(f"CRD {EVALHUB_CRD_NAME} declares no versions", pytrace=False)
    return versions[0].name


def _evalhub_resource(dynamic_client: DynamicClient, api_version: str) -> Any:
    return dynamic_client.resources.get(
        api_version=f"{EVALHUB_API_GROUP}/{api_version}",
        kind=EVALHUB_KIND,
    )


def read_evalhub(dynamic_client: DynamicClient, api_version: str, name: str, namespace: str) -> dict[str, Any]:
    """Return the named EvalHub resource as a dict."""
    LOGGER.info("reading %s/%s in %s as %s", EVALHUB_KIND, name, namespace, api_version)
    try:
        return _evalhub_resource(dynamic_client, api_version).get(name=name, namespace=namespace).to_dict()
    except NotFoundError:
        pytest.fail(
            f"{EVALHUB_KIND}/{name} was not found in {namespace}; run 'k8s-tests up' or 'k8s-tests deploy' first",
            pytrace=False,
        )


def create_evalhub_instance(
    dynamic_client: DynamicClient,
    api_version: str,
    name: str,
    namespace: str,
    replicas: int = 1,
    database: str = EVALHUB_DATABASE_BACKEND,
) -> dict[str, Any]:
    """Create an EvalHub resource from Python, the way a user would."""
    body = {
        "apiVersion": f"{EVALHUB_API_GROUP}/{api_version}",
        "kind": EVALHUB_KIND,
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"replicas": replicas, "database": {"type": database}},
    }
    LOGGER.info("creating %s/%s in %s with %s database", EVALHUB_KIND, name, namespace, database)
    return _evalhub_resource(dynamic_client, api_version).create(body=body, namespace=namespace).to_dict()


def evalhub_conditions(evalhub: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map the EvalHub resource's status conditions by type."""
    status = evalhub.get("status") or {}
    return {condition.get("type"): condition for condition in status.get("conditions") or []}


def evalhub_condition(evalhub: dict[str, Any], condition_type: str) -> str | None:
    """Return the status string of an EvalHub condition, if it is reported."""
    condition = evalhub_conditions(evalhub).get(condition_type)
    return condition.get("status") if condition else None


def describe_evalhub_conditions(evalhub: dict[str, Any]) -> str:
    """Readable one-line summary of every EvalHub condition."""
    conditions = evalhub_conditions(evalhub)
    if not conditions:
        return "none"
    return ", ".join(
        f"{condition_type}={condition.get('status')} ({condition.get('reason')})"
        for condition_type, condition in sorted(conditions.items())
    )


def wait_for_evalhub_condition(
    dynamic_client: DynamicClient,
    api_version: str,
    name: str,
    namespace: str,
    condition_type: str,
    status: str = "True",
    timeout: int = DEFAULT_WAIT_TIMEOUT,
) -> dict[str, Any]:
    """Poll the EvalHub resource until `condition_type` reports `status`."""
    deadline = time.monotonic() + timeout
    observed: dict[str, Any] = {}
    while True:
        observed = read_evalhub(dynamic_client, api_version, name, namespace)
        if evalhub_condition(observed, condition_type) == status:
            LOGGER.info("%s/%s %s=%s", EVALHUB_KIND, name, condition_type, status)
            return observed
        if time.monotonic() >= deadline:
            break
        LOGGER.info(
            "waiting for %s=%s on %s/%s; conditions: %s",
            condition_type,
            status,
            EVALHUB_KIND,
            name,
            describe_evalhub_conditions(observed),
        )
        time.sleep(WAIT_INTERVAL)
    pytest.fail(
        f"{EVALHUB_KIND}/{name} in {namespace} did not report {condition_type}={status} within {timeout}s; "
        f"conditions: {describe_evalhub_conditions(observed)}",
        pytrace=False,
    )


def create_namespace(core_client: client.CoreV1Api, name: str, labels: dict[str, str] | None = None) -> str:
    """Create a namespace, optionally labelled, and return its name."""
    body = client.V1Namespace(metadata=client.V1ObjectMeta(name=name, labels=labels or {}))
    core_client.create_namespace(body=body)
    LOGGER.info("created namespace %s with labels %s", name, labels or {})
    return name


def release_namespace(core_client: client.CoreV1Api, name: str) -> None:
    """Delete a namespace the tests created, or leave it for inspection."""
    if keep_resources():
        LOGGER.warning(
            "keeping namespace %s: kubectl -n %s get all,cm,evalhubs,jobs",
            name,
            name,
        )
        return
    delete_namespace(core_client, name)


def delete_namespace(core_client: client.CoreV1Api, name: str) -> None:
    """Delete a namespace, tolerating one that is already gone."""
    try:
        core_client.delete_namespace(name=name)
    except ApiException as exc:
        if exc.status != 404:
            raise
    LOGGER.info("deleted namespace %s", name)


def seed_instance_tls(core_client: client.CoreV1Api, name: str, namespace: str) -> None:
    """Create the TLS material a self-managed EvalHub instance needs on Kind.

    The operator mounts `<name>-tls` into its kube-rbac-proxy sidecar and reads
    the upstream CA from the `<name>-service-ca` ConfigMap it creates with no
    data, expecting the OpenShift service CA to fill it in. Kind has no service
    CA, so the tests mint the certificate and fill both in.
    """
    cert_pem, key_pem = generate_serving_cert(dns_names=service_dns_names(service=name, namespace=namespace))
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(name=f"{name}-tls", namespace=namespace),
        type="kubernetes.io/tls",
        data={
            "tls.crt": base64.b64encode(cert_pem).decode(),
            "tls.key": base64.b64encode(key_pem).decode(),
        },
    )
    core_client.create_namespaced_secret(namespace=namespace, body=secret)

    config_map = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=f"{name}-service-ca", namespace=namespace),
        data={"service-ca.crt": cert_pem.decode()},
    )
    core_client.create_namespaced_config_map(namespace=namespace, body=config_map)
    LOGGER.info("seeded %s-tls and %s-service-ca in %s", name, name, namespace)


def tenant_config_maps(core_client: client.CoreV1Api, namespace: str) -> set[str]:
    """Names of the EvalHub ConfigMaps present in a namespace."""
    names = {item.metadata.name for item in core_client.list_namespaced_config_map(namespace=namespace).items}
    LOGGER.info("%s ConfigMaps: %s", namespace, sorted(names))
    return names


def container_image(deployment: client.V1Deployment, container_name: str) -> str | None:
    """Image of the named container in a Deployment's pod template."""
    for container in deployment.spec.template.spec.containers or []:
        if container.name == container_name:
            return container.image
    return None


@contextlib.contextmanager
def port_forward(context: str, namespace: str, target: str, local_port: int, remote_port: int) -> Iterator[str]:
    """Forward `local_port` to EvalHub's TLS port and yield the local HTTPS base URL."""
    with _forward_port(
        context=context,
        namespace=namespace,
        target=target,
        local_port=local_port,
        remote_port=remote_port,
        scheme="https",
    ) as url:
        yield url


def request(
    method: str,
    url: str,
    timeout: int = 10,
    headers: dict[str, str] | None = None,
    json_body: Any | None = None,
    params: dict[str, str] | None = None,
) -> requests.Response:
    """Send a request to EvalHub without verifying the certificate the tests minted."""
    LOGGER.info("%s %s", method.upper(), url)
    return requests.request(method, url, headers=headers, json=json_body, params=params, verify=False, timeout=timeout)


def instance_tls_cert(core_client: client.CoreV1Api, namespace: str, name: str = EVALHUB_NAME) -> bytes:
    """The certificate the instance's kube-rbac-proxy serves, from its `<name>-tls` Secret."""
    secret = core_client.read_namespaced_secret(name=f"{name}-tls", namespace=namespace)
    return base64.b64decode((secret.data or {})["tls.crt"])


def seed_tenant_ca(core_client: client.CoreV1Api, tenant_namespace: str, cert_pem: bytes, timeout: int = 300) -> None:
    """Fill the tenant's `<name>-service-ca` ConfigMap with the instance certificate.

    The operator creates that ConfigMap with no data, expecting the OpenShift
    service CA to fill it in; the evaluation sidecar reads `service-ca.crt` from it
    to verify the instance it reports to. Kind has no service CA, so the tests put
    the certificate the instance actually serves there.
    """
    config_map_name = f"{EVALHUB_NAME}-service-ca"
    deadline = time.monotonic() + timeout
    while True:
        try:
            core_client.read_namespaced_config_map(name=config_map_name, namespace=tenant_namespace)
            break
        except ApiException as exc:
            if exc.status != 404:
                raise
        if time.monotonic() >= deadline:
            pytest.fail(
                f"the operator never created {tenant_namespace}/{config_map_name}, "
                "so the evaluation sidecar has no CA to verify the instance with",
                pytrace=False,
            )
        time.sleep(WAIT_INTERVAL)
    core_client.patch_namespaced_config_map(
        name=config_map_name,
        namespace=tenant_namespace,
        body={"data": {"service-ca.crt": cert_pem.decode()}},
    )
    LOGGER.info("seeded %s/%s with the instance certificate", tenant_namespace, config_map_name)


def wait_for_tenant_rbac(
    rbac_client: client.RbacAuthorizationV1Api,
    tenant_namespace: str,
    instance_namespace: str,
    timeout: int = TENANT_RBAC_TIMEOUT,
) -> None:
    """Wait until the operator has granted the instance's API access to the tenant.

    A tenant namespace created after the instance is ready is provisioned by the
    operator's namespace watch, which also binds the roles the API needs to write
    the job's spec ConfigMap and Job into the tenant. Submitting before that lands
    makes the run fail with a permission error that says nothing about the code
    under test.

    The binding is checked by subject, not only by name: every instance is called
    `evalhub`, so a second instance pointed at the same tenant would otherwise
    satisfy a name check while binding its own ServiceAccount instead of this one.
    """
    api_service_account = f"{EVALHUB_NAME}-service"
    deadline = time.monotonic() + timeout
    names: list[str] = []
    while True:
        bindings = rbac_client.list_namespaced_role_binding(namespace=tenant_namespace).items
        names = [binding.metadata.name for binding in bindings]
        for binding in bindings:
            if not binding.metadata.name.endswith("-job-config-rb"):
                continue
            subjects = {(s.namespace, s.name) for s in binding.subjects or []}
            if (instance_namespace, api_service_account) in subjects:
                # Read it again after a pause: another EvalHub instance in the
                # cluster rewrites these subjects on its own reconcile, so a
                # single observation can be a binding that is about to flip back.
                time.sleep(WAIT_INTERVAL)
                bindings = rbac_client.list_namespaced_role_binding(namespace=tenant_namespace).items
                current = {
                    (s.namespace, s.name)
                    for item in bindings
                    if item.metadata.name == binding.metadata.name
                    for s in item.subjects or []
                }
                if (instance_namespace, api_service_account) in current:
                    LOGGER.info("%s/%s may write job configmaps", tenant_namespace, api_service_account)
                    return
        if time.monotonic() >= deadline:
            break
        time.sleep(WAIT_INTERVAL)
    pytest.fail(
        f"the operator did not grant {instance_namespace}/{api_service_account} access to tenant namespace "
        f"{tenant_namespace} within {timeout}s; role bindings present: {sorted(names) or 'none'}",
        pytrace=False,
    )


def grant_tenant_job_access(
    rbac_client: client.RbacAuthorizationV1Api,
    tenant_namespace: str,
    instance_namespace: str,
    instance_name: str = EVALHUB_NAME,
) -> None:
    """Bind the instance's API ServiceAccount to the roles a job needs in the tenant.

    The operator creates those bindings too, but every instance is called `evalhub`
    and each one rewrites the subjects of the same binding on its own reconcile, so
    an instance left behind by an earlier run - exactly what `KEEP_RESOURCES`
    leaves - can take the grant away between the check that saw it and the job that
    needs it. These bindings carry a name the operator does not manage, so they
    stay put and keep the run independent of who reconciled last.
    """
    subjects = [
        client.RbacV1Subject(
            kind="ServiceAccount",
            name=f"{instance_name}-service",
            namespace=instance_namespace,
        )
    ]
    for role in TENANT_JOB_ACCESS_ROLES:
        name = f"{TENANT_JOB_ACCESS_BINDING}-{role.rsplit('-', 1)[-1]}"
        body = client.V1RoleBinding(
            metadata=client.V1ObjectMeta(name=name, namespace=tenant_namespace),
            role_ref=client.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name=role),
            subjects=subjects,
        )
        try:
            rbac_client.create_namespaced_role_binding(namespace=tenant_namespace, body=body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            rbac_client.replace_namespaced_role_binding(name=name, namespace=tenant_namespace, body=body)
        LOGGER.info(
            "granted %s/%s-service the %s role in %s",
            instance_namespace,
            instance_name,
            role,
            tenant_namespace,
        )


def create_tenant_identity(
    core_client: client.CoreV1Api,
    rbac_client: client.RbacAuthorizationV1Api,
    tenant_namespace: str,
    name: str = TENANT_IDENTITY_NAME,
    rules: dict[str, list[str]] | None = None,
) -> str:
    """Create the ServiceAccount a tenant's user acts as, and grant it the API.

    The API checks call EvalHub as this identity: the proxy's SubjectAccessReviews
    run in the namespace named by `X-Tenant`, so the grant has to live there.

    `rules` narrows the grant to the pseudo-resources named and the verbs listed,
    which is how a check acts as the tenant-user Role the operator writes rather
    than as an identity with every verb. The default is the broad grant the EvalHub
    API checks need.
    """
    service_account = client.V1ServiceAccount(metadata=client.V1ObjectMeta(name=name, namespace=tenant_namespace))
    if rules is None:
        api_rules = [
            client.V1PolicyRule(
                api_groups=[EVALHUB_API_GROUP],
                resources=list(TENANT_API_RESOURCES),
                verbs=TENANT_API_VERBS,
            )
        ]
    else:
        api_rules = [
            client.V1PolicyRule(api_groups=[EVALHUB_API_GROUP], resources=[resource], verbs=list(verbs))
            for resource, verbs in rules.items()
        ]
    role = client.V1Role(
        metadata=client.V1ObjectMeta(name=name, namespace=tenant_namespace),
        rules=[
            *api_rules,
            # A job submission also asks for the MLflow experiment the run records into.
            client.V1PolicyRule(
                api_groups=["mlflow.kubeflow.org"],
                resources=["experiments"],
                verbs=["create", "get"],
            ),
        ],
    )
    binding = client.V1RoleBinding(
        metadata=client.V1ObjectMeta(name=name, namespace=tenant_namespace),
        role_ref=client.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=name),
        subjects=[client.RbacV1Subject(kind="ServiceAccount", name=name, namespace=tenant_namespace)],
    )
    for create, body, replace in (
        (
            core_client.create_namespaced_service_account,
            service_account,
            core_client.replace_namespaced_service_account,
        ),
        (rbac_client.create_namespaced_role, role, rbac_client.replace_namespaced_role),
        (rbac_client.create_namespaced_role_binding, binding, rbac_client.replace_namespaced_role_binding),
    ):
        kind = body.kind
        try:
            create(namespace=tenant_namespace, body=body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            replace(name=body.metadata.name, namespace=tenant_namespace, body=body)
        LOGGER.info("ensured %s %s/%s", kind, tenant_namespace, body.metadata.name)
    return name


def api_headers(token: str, tenant: str) -> dict[str, str]:
    """Headers the proxy authorizes with: a bearer token and the tenant namespace."""
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant}


def submit_job(base_url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    """Submit an evaluation job and return the resource the server created."""
    LOGGER.info("submitting job %r with headers %s", payload.get("name"), sorted(headers))
    response = requests.post(
        f"{base_url}{EVALHUB_JOBS_PATH}",
        headers=headers,
        json=payload,
        verify=False,
        timeout=timeout,
    )
    if response.status_code not in (200, 201, 202):
        pytest.fail(
            f"POST {EVALHUB_JOBS_PATH} answered {response.status_code}: {response.text[:1000]}",
            pytrace=False,
        )
    job = response.json()
    LOGGER.info("job %s submitted", (job.get("resource") or {}).get("id"))
    return job


def read_job(base_url: str, headers: dict[str, str], job_id: str, timeout: int = 30) -> dict[str, Any]:
    """Read one evaluation job."""
    return request("get", f"{base_url}{EVALHUB_JOBS_PATH}/{job_id}", timeout=timeout, headers=headers).json()


def wait_for_job(
    base_url: str,
    headers: dict[str, str],
    job_id: str,
    timeout: int = 900,
    interval: int = 10,
) -> dict[str, Any]:
    """Poll a job until it reaches a terminal state, logging every transition."""
    deadline = time.monotonic() + timeout
    last_state = None
    while True:
        job = read_job(base_url, headers, job_id)
        state = (job.get("status") or {}).get("state")
        if state != last_state:
            message = ((job.get("status") or {}).get("message") or {}).get("message")
            LOGGER.info("job %s is %s: %s", job_id, state, message)
            last_state = state
        if state in JOB_TERMINAL_STATES:
            return job
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    pytest.fail(f"job {job_id} did not reach a terminal state within {timeout}s (last: {last_state})", pytrace=False)


def create_provider(
    base_url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int = 30
) -> dict[str, Any]:
    """Create a custom provider and return the resource the server created."""
    response = request(
        "post", f"{base_url}{EVALHUB_PROVIDERS_PATH}", timeout=timeout, headers=headers, json_body=payload
    )
    if response.status_code != 201:
        pytest.fail(
            f"POST {EVALHUB_PROVIDERS_PATH} answered {response.status_code}: {response.text[:1000]}",
            pytrace=False,
        )
    provider = response.json()
    LOGGER.info("provider %s created", (provider.get("resource") or {}).get("id"))
    return provider


def delete_provider(base_url: str, headers: dict[str, str], provider_id: str, timeout: int = 30) -> None:
    """Delete a custom provider."""
    response = request("delete", f"{base_url}{EVALHUB_PROVIDERS_PATH}/{provider_id}", timeout=timeout, headers=headers)
    if response.status_code not in (200, 202, 204):
        pytest.fail(
            f"DELETE {EVALHUB_PROVIDERS_PATH}/{provider_id} answered {response.status_code}: {response.text[:1000]}",
            pytrace=False,
        )
    LOGGER.info("provider %s deleted", provider_id)


def delete_job(
    base_url: str, headers: dict[str, str], job_id: str, *, hard_delete: bool = True, timeout: int = 30
) -> None:
    """Delete an evaluation job and, with `hard_delete`, its runtime resources."""
    response = request(
        "delete",
        f"{base_url}{EVALHUB_JOBS_PATH}/{job_id}",
        timeout=timeout,
        headers=headers,
        params={"hard_delete": "true"} if hard_delete else None,
    )
    if response.status_code not in (200, 202, 204):
        pytest.fail(
            f"DELETE {EVALHUB_JOBS_PATH}/{job_id} answered {response.status_code}: {response.text[:1000]}",
            pytrace=False,
        )
    LOGGER.info("job %s deleted (hard_delete: %s)", job_id, hard_delete)


def benchmark_result(job: dict[str, Any], benchmark_id: str) -> dict[str, Any]:
    """The result block for one benchmark, failing with what was reported instead."""
    results = job.get("results") or {}
    benchmarks = results.get("benchmarks") or []
    for benchmark in benchmarks:
        if benchmark.get("id") == benchmark_id:
            return benchmark
    pytest.fail(
        f"job {((job.get('resource') or {}).get('id'))} reported no result for {benchmark_id!r}; results: {results}",
        pytrace=False,
    )


def patch_instance_spec(
    dynamic_client: DynamicClient,
    api_version: str,
    name: str,
    namespace: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Merge top-level keys into an EvalHub instance's spec in one patch.

    One patch is one reconcile, and therefore one rollout: an env value and a provider
    list patched separately would roll the Pod twice and leave the checks racing the
    second one.
    """
    resource = _evalhub_resource(dynamic_client, api_version)
    LOGGER.info("patching %s/%s spec with %s", name, namespace, sorted(spec))
    return resource.patch(
        name=name,
        namespace=namespace,
        body={"spec": spec},
        content_type="application/merge-patch+json",
    ).to_dict()


def wait_for_container_env(
    apps_client: client.AppsV1Api,
    name: str,
    namespace: str,
    container_name: str,
    env_name: str,
    expected: str,
    timeout: int = DEFAULT_WAIT_TIMEOUT,
    interval: int = 5,
) -> None:
    """Wait until the Deployment the operator rolled carries an env value.

    Changing `spec.env` makes the operator rewrite the Deployment, so the value is only
    in effect once the new pod template is the one that is available; a test that reads
    the CR alone would race the rollout.
    """
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        deployment = apps_client.read_namespaced_deployment(name=name, namespace=namespace)
        # The value and the rollout status have to come from the same snapshot: a value
        # read from an earlier template must not satisfy a rollout of a later one.
        last = ""
        containers = ((deployment.spec.template.spec or None) and deployment.spec.template.spec.containers) or []
        for container in containers:
            if container.name != container_name:
                continue
            for env in container.env or []:
                if env.name == env_name:
                    last = env.value or ""
        status = deployment.status
        # A surplus Pod means the rollout is not finished: an old Pod that is still
        # serving keeps `available_replicas` at the replica count while the new template's
        # Pod is not ready, which is exactly how a crash-looping replacement hides.
        rolled = (
            status.updated_replicas == deployment.spec.replicas
            and status.ready_replicas == deployment.spec.replicas
            and status.available_replicas == deployment.spec.replicas
            and status.replicas == deployment.spec.replicas
            and (status.observed_generation or 0) >= (deployment.metadata.generation or 0)
        )
        if last == expected and rolled:
            LOGGER.info("%s/%s container %s runs with %s=%s", namespace, name, container_name, env_name, expected)
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"{namespace}/{name} did not roll out with {env_name}={expected} within {timeout}s "
                f"(last value {last!r})",
                pytrace=False,
            )
        time.sleep(interval)
