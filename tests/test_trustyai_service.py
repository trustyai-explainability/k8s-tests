"""Checks for the TrustyAI service: the resource, its operand and the API.

The service is created by the fixtures, so a run starts from nothing and leaves
nothing behind. Two platform-provided pieces are seeded in place of the OpenShift
equivalents: the serving certificates the proxies mount, and the caller identity
the proxy authorizes. What Kind cannot provide at all — a router that admits a
Route, and KServe InferenceServices to register — is asserted as the resource
reports it, not faked.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest
import requests
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from utilities.kube import mint_token, read_deployment, wait_for_deployment_available
from utilities.release import release
from utilities.trustyai_service import (
    API_VERSIONS,
    APP_CONTAINER,
    CRD_NAME,
    DRIFT_PATH,
    DRIFT_REQUESTS_PATH,
    DRIFT_SCHEDULE_PATH,
    INFO_PATH,
    INTERNAL_PROXY_CONTAINER,
    METRICS_SCHEDULE,
    MODEL_NAME,
    PRODUCTION_ROWS,
    PRODUCTION_SHIFT,
    PROXY_CONTAINER,
    SERVICE_CONTAINERS,
    SERVICE_NAME,
    STORAGE_FOLDER,
    STORAGE_SIZE,
    TRAINING_ROWS,
    UPLOAD_PATH,
    ServiceAddress,
    condition,
    describe_conditions,
    drift_request,
    observations,
    read_service,
    rows,
    upload_payload,
)

LOGGER = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def trustyai_service(
    request: pytest.FixtureRequest,
    trustyai_services: Callable[..., ServiceAddress],
) -> ServiceAddress:
    """The service this module checks, created the way any check can create its own.

    `trustyai_services` gives each caller a workload with a namespace of its own; this
    module asks for one and reuses it, because the operand takes minutes to roll out
    and every check here looks at the same one. A check that needs a second service -
    a second namespace, a different spec - calls the fixture again.
    """
    return trustyai_services(request)


def _container(deployment: client.V1Deployment, name: str) -> client.V1Container:
    return next(container for container in deployment.spec.template.spec.containers if container.name == name)


@pytest.mark.service
def test_service_crd_serves_both_versions(api_client: client.ApiClient) -> None:
    """The CRD serves v1 and v1alpha1, with v1 as the storage version."""
    crd = client.ApiextensionsV1Api(api_client).read_custom_resource_definition(name=CRD_NAME)
    served = {version.name for version in crd.spec.versions if version.served}
    storage = {version.name for version in crd.spec.versions if version.storage}
    LOGGER.info("%s serves %s, stores %s", CRD_NAME, sorted(served), sorted(storage))
    assert served >= set(API_VERSIONS), f"served versions are {sorted(served)}, expected {sorted(API_VERSIONS)}"
    assert storage == {"v1"}, f"storage version is {sorted(storage)}, expected ['v1']"


@pytest.mark.service
def test_service_crd_conversion_uses_a_webhook(api_client: client.ApiClient) -> None:
    """The two versions are converted by a webhook, so neither is a second schema."""
    crd = client.ApiextensionsV1Api(api_client).read_custom_resource_definition(name=CRD_NAME)
    conversion = crd.spec.conversion
    strategy = conversion.strategy if conversion else None
    LOGGER.info("%s conversion strategy: %s", CRD_NAME, strategy)
    assert strategy == "Webhook", f"conversion strategy is {strategy}, expected Webhook"
    assert conversion.webhook is not None, "the CRD names no conversion webhook"
    assert conversion.webhook.client_config, "the conversion webhook has no client configuration"


@pytest.mark.service
def test_service_reads_through_both_versions(
    custom_api: client.CustomObjectsApi, trustyai_service: ServiceAddress
) -> None:
    """A resource created as v1 is readable as v1alpha1, with the same spec."""
    specs = {
        version: read_service(custom_api, trustyai_service.namespace, trustyai_service.name, version).get("spec")
        for version in API_VERSIONS
    }
    LOGGER.info("read %s/%s as %s", trustyai_service.namespace, trustyai_service.name, sorted(specs))
    assert all(specs.values()), f"a version returned no spec: {specs}"
    assert specs["v1"] == specs["v1alpha1"], f"the versions disagree: {specs}"


@pytest.mark.service
@release(min="3.6")
@pytest.mark.usefixtures("modular_trustyai_architecture")
def test_service_reports_the_components_kind_does_not_provide(
    custom_api: client.CustomObjectsApi, trustyai_service: ServiceAddress
) -> None:
    """The resource names the platform components missing here, and nothing else.

    `Ready` needs the Route an OpenShift router would admit and the KServe
    InferenceServices a model server would register. Neither exists on Kind, so
    what is asserted is that those two are the only conditions reported false
    while the parts the operator can deliver — the claim and the operand — are
    reported available.
    """
    service = read_service(custom_api, trustyai_service.namespace, trustyai_service.name)
    LOGGER.info("conditions: %s", describe_conditions(service))

    pvc = condition(service, "PVCAvailable")
    assert pvc is not None and pvc.get("status") == "True", f"PVCAvailable is not True: {pvc}"

    route = condition(service, "RouteAvailable")
    assert route is not None, "the resource reports no RouteAvailable condition"
    if route.get("status") != "True":
        assert route.get("reason") == "RouteNotFound", f"RouteAvailable reason is {route.get('reason')}"

    ready = condition(service, "Ready")
    assert ready is not None, "the resource reports no Ready condition"
    if ready.get("status") != "True":
        assert ready.get("reason") == "ComponentsNotReady", f"Ready reason is {ready.get('reason')}"


@pytest.mark.service
@release(min="3.6")
@pytest.mark.usefixtures("modular_trustyai_architecture")
def test_service_operand_is_rolled_out(
    core_client: client.CoreV1Api, apps_client: client.AppsV1Api, trustyai_service: ServiceAddress
) -> None:
    """The operand runs one replica with every container the service needs."""
    deployment = read_deployment(apps_client, SERVICE_NAME, trustyai_service.namespace)
    containers = [container.name for container in deployment.spec.template.spec.containers]
    LOGGER.info("%s/%s containers: %s", trustyai_service.namespace, SERVICE_NAME, containers)
    assert sorted(containers) == sorted(SERVICE_CONTAINERS), f"containers are {containers}"
    assert deployment.spec.replicas == 1, f"the operand asks for {deployment.spec.replicas} replicas"
    # Idempotent: the fixture already waited, and this reports the operand's health
    # rather than a single condition.
    wait_for_deployment_available(apps_client, SERVICE_NAME, trustyai_service.namespace, core_client=core_client)


@pytest.mark.service
def test_service_operand_runs_the_requested_image(
    apps_client: client.AppsV1Api, trustyai_service: ServiceAddress, trustyai_service_image: str
) -> None:
    """The Deployment runs the image the operator was told to deploy."""
    if not trustyai_service_image:
        pytest.skip("TRUSTYAI_SERVICE_IMG is not set, so no operand image was requested for this run")
    app = _container(read_deployment(apps_client, SERVICE_NAME, trustyai_service.namespace), APP_CONTAINER)
    LOGGER.info("%s runs %s", APP_CONTAINER, app.image)
    assert app.image == trustyai_service_image, f"{APP_CONTAINER} runs {app.image}, expected {trustyai_service_image}"


@pytest.mark.service
def test_service_claim_is_bound_and_mounted(
    core_client: client.CoreV1Api, apps_client: client.AppsV1Api, trustyai_service: ServiceAddress
) -> None:
    """The claim the operator creates is bound at the requested size, and mounted."""
    claims = core_client.list_namespaced_persistent_volume_claim(namespace=trustyai_service.namespace).items
    LOGGER.info("%s PVCs: %s", trustyai_service.namespace, [(item.metadata.name, item.status.phase) for item in claims])
    assert claims, "the operator created no PersistentVolumeClaim"
    claim = claims[0]
    assert claim.status.phase == "Bound", f"PVC {claim.metadata.name} is {claim.status.phase}"
    requested = claim.spec.resources.requests["storage"]
    assert requested == STORAGE_SIZE, f"PVC {claim.metadata.name} asks for {requested}, expected {STORAGE_SIZE}"
    app = _container(read_deployment(apps_client, SERVICE_NAME, trustyai_service.namespace), APP_CONTAINER)
    mounts = {mount.mount_path for mount in app.volume_mounts or []}
    assert STORAGE_FOLDER in mounts, f"{APP_CONTAINER} mounts {sorted(mounts)}, expected {STORAGE_FOLDER}"


@pytest.mark.service
def test_service_operand_carries_the_resource_settings(
    apps_client: client.AppsV1Api, trustyai_service: ServiceAddress
) -> None:
    """The schedule and storage settings from the resource reach the operand."""
    app = _container(read_deployment(apps_client, SERVICE_NAME, trustyai_service.namespace), APP_CONTAINER)
    environment = {entry.name: entry.value for entry in app.env or []}
    LOGGER.info("%s environment keys: %s", APP_CONTAINER, sorted(environment))
    assert environment.get("SERVICE_METRICS_SCHEDULE") == METRICS_SCHEDULE, (
        f"SERVICE_METRICS_SCHEDULE is {environment.get('SERVICE_METRICS_SCHEDULE')}, expected {METRICS_SCHEDULE}"
    )
    assert environment.get("SERVICE_STORAGE_FORMAT") == "PVC", (
        f"SERVICE_STORAGE_FORMAT is {environment.get('SERVICE_STORAGE_FORMAT')}, expected PVC"
    )


@pytest.mark.service
def test_health_requires_no_credentials(trustyai_service: ServiceAddress) -> None:
    """The app's health endpoint answers on the internal Service, unauthenticated."""
    response = trustyai_service.health()
    LOGGER.info("health response: %s %s", response.status_code, response.text)
    assert response.status_code == 200, f"/q/health returned {response.status_code}: {response.text}"
    body = response.json()
    assert body.get("status") == "healthy", f"health status is {body.get('status')}: {body}"
    failing = [
        check for checks in (body.get("checks") or {}).values() for check in checks if check.get("status") != "ok"
    ]
    assert not failing, f"health checks are not ok: {failing}"


@pytest.mark.service
def test_api_rejects_a_request_without_credentials(trustyai_service: ServiceAddress) -> None:
    """The proxy authorizes API requests by SubjectAccessReview, so none is anonymous."""
    response = trustyai_service.call("GET", INFO_PATH, authenticated=False)
    LOGGER.info("unauthenticated %s response: %s", INFO_PATH, response.status_code)
    assert response.status_code in (401, 403), f"expected 401 or 403, got {response.status_code}: {response.text}"


@pytest.mark.service
def test_api_rejects_a_token_without_the_grant(
    core_client: client.CoreV1Api, kube_context: str, trustyai_service: ServiceAddress
) -> None:
    """A real token for an identity with no grant on the Service is refused."""
    outsider = "trustyai-outsider"
    try:
        core_client.create_namespaced_service_account(
            namespace=trustyai_service.namespace,
            body=client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name=outsider, namespace=trustyai_service.namespace)
            ),
        )
    except ApiException as exc:
        if exc.status != 409:
            raise
    token = mint_token(kube_context, trustyai_service.namespace, outsider)
    LOGGER.info("calling %s as %s, which has no Role", INFO_PATH, outsider)
    response = requests.get(
        f"{trustyai_service.api_url}{INFO_PATH}",
        headers={"Authorization": f"Bearer {token}"},
        verify=str(trustyai_service.ca_file),
        timeout=60,
    )
    LOGGER.info("answer for an unauthorised identity: %s", response.status_code)
    assert response.status_code in (401, 403), f"expected 401 or 403, got {response.status_code}: {response.text}"


@pytest.mark.service
@release(min="3.6")
def test_api_returns_model_metadata(trustyai_service: ServiceAddress) -> None:
    """`/info` answers with the metadata document a caller reads before uploading."""
    response = trustyai_service.call("GET", INFO_PATH)
    LOGGER.info("info response: %s %s", response.status_code, response.text[:200])
    assert response.status_code == 200, f"/info returned {response.status_code}: {response.text}"
    assert isinstance(response.json(), dict), f"/info returned {type(response.json())}, expected an object"


@pytest.mark.service
@release(min="3.6")
def test_upload_registers_the_observations(trustyai_service: ServiceAddress) -> None:
    """Both halves of the drift comparison can be uploaded, and are counted."""
    before = observations(trustyai_service.call("GET", INFO_PATH).json())
    payloads = {
        "TRAINING": upload_payload("TRAINING", rows(TRAINING_ROWS)),
        "PRODUCTION": upload_payload("PRODUCTION", rows(PRODUCTION_ROWS, PRODUCTION_SHIFT)),
    }
    for tag, payload in payloads.items():
        response = trustyai_service.call("POST", UPLOAD_PATH, payload)
        LOGGER.info("%s upload: %s %s", tag, response.status_code, response.text[:200])
        assert response.status_code == 200, f"{tag} upload returned {response.status_code}: {response.text}"
    after = observations(trustyai_service.call("GET", INFO_PATH).json())
    expected = before + TRAINING_ROWS + PRODUCTION_ROWS
    LOGGER.info("%s observations went from %d to %d", MODEL_NAME, before, after)
    assert after == expected, f"{MODEL_NAME} reports {after} observations, expected {expected}"


@pytest.mark.service
@release(min="3.6")
def test_drift_detects_the_shifted_distribution(trustyai_service: ServiceAddress) -> None:
    """CompareMeans separates the TRAINING reference from the shifted production rows."""
    response = trustyai_service.call("POST", DRIFT_PATH, drift_request())
    LOGGER.info("drift response: %s %s", response.status_code, response.text[:300])
    assert response.status_code == 200, f"{DRIFT_PATH} returned {response.status_code}: {response.text}"
    result = response.json()
    assert isinstance(result.get("value"), (int, float)), f"the drift value is {result.get('value')}"
    assert result.get("drift_detected") is True, f"drift was not detected: {result}"


@pytest.mark.service
@release(min="3.6")
def test_metric_schedule_is_listed_and_can_be_deleted(trustyai_service: ServiceAddress) -> None:
    """A recurring metric request is scheduled, listed exactly once, then removed."""
    scheduled = trustyai_service.call("POST", DRIFT_SCHEDULE_PATH, drift_request())
    LOGGER.info("schedule response: %s %s", scheduled.status_code, scheduled.text[:200])
    assert scheduled.status_code == 200, f"{DRIFT_SCHEDULE_PATH} returned {scheduled.status_code}: {scheduled.text}"
    request_id = scheduled.json().get("requestId")
    assert request_id, f"the schedule response carries no requestId: {scheduled.text}"

    listed = trustyai_service.call("GET", DRIFT_REQUESTS_PATH)
    assert listed.status_code == 200, f"{DRIFT_REQUESTS_PATH} returned {listed.status_code}: {listed.text}"
    matches = [entry for entry in listed.json().get("requests") or [] if entry.get("requestId") == request_id]
    LOGGER.info("%s lists %d match(es) for %s", DRIFT_REQUESTS_PATH, len(matches), request_id)
    assert len(matches) == 1, f"the metric request is listed {len(matches)} time(s), expected once"

    deleted = trustyai_service.call("DELETE", DRIFT_SCHEDULE_PATH, {"requestId": request_id})
    LOGGER.info("delete response: %s %s", deleted.status_code, deleted.text[:200])
    assert deleted.status_code in (200, 204), f"deleting {request_id} returned {deleted.status_code}"


@pytest.mark.service
@release(min="3.6")
@pytest.mark.usefixtures("modular_trustyai_architecture")
def test_internal_proxy_keeps_the_consumer_path_unauthenticated(
    apps_client: client.AppsV1Api, trustyai_service: ServiceAddress
) -> None:
    """The consumer proxy a model server talks to lets its own paths through.

    KServe reaches the service over the internal proxy, which ignores the paths
    the consumer contract serves; a service without that would pass every other
    check here and still be unusable for its main job.
    """
    deployment = read_deployment(apps_client, SERVICE_NAME, trustyai_service.namespace)
    internal = _container(deployment, INTERNAL_PROXY_CONTAINER)
    args = " ".join(internal.args or [])
    ports = {port.name: port.container_port for port in internal.ports or []}
    LOGGER.info("%s ports %s, args %s", INTERNAL_PROXY_CONTAINER, ports, args)
    assert "internal-https" in ports, f"{INTERNAL_PROXY_CONTAINER} exposes {sorted(ports)}"
    ignored = next((arg.split("=", 1)[1] for arg in internal.args or [] if arg.startswith("--ignore-paths=")), "")
    ignored_paths = {path for path in ignored.split(",") if path}
    LOGGER.info("paths served without authorization: %s", sorted(ignored_paths))
    assert "/consumer/kserve/v2" in ignored_paths, f"the consumer path is authorized: {sorted(ignored_paths)}"
    proxy = _container(read_deployment(apps_client, SERVICE_NAME, trustyai_service.namespace), PROXY_CONTAINER)
    LOGGER.info("%s args: %s", PROXY_CONTAINER, proxy.args)
    assert any("--secure-listen-address" in arg for arg in proxy.args or []), f"{PROXY_CONTAINER} args: {proxy.args}"
