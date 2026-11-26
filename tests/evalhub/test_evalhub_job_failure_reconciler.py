"""Exercise the operator's failure reporting with real kubelet image failures.

Two shapes are covered: an adapter image whose tag does not exist, which kubelet
keeps retrying (ImagePullBackOff) and the operator only reports once the
ten-minute grace period trustyai-service-operator#1139 added has passed - first
shipped in release 3.7 - and a malformed image reference (InvalidImageName),
which is fatal without any grace period and is covered from release 3.6 as a
regression check. Both must end with the benchmark failed in EvalHub, the
reason in its error message, and the Kubernetes Job promoted to Failed.

The grace case is its own section because it waits out most of the operator's
ten minutes; the invalid-image-name case is quick and stays in the default run.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from uuid import uuid4

import pytest
from kubernetes import client

from utilities.evalhub import (
    WAIT_INTERVAL,
    api_headers,
    create_provider,
    delete_job,
    delete_provider,
    instance_tls_cert,
    read_job,
    submit_job,
    wait_for_container_env,
    wait_for_job,
)
from utilities.kube import keep_resources
from utilities.model import ModelEndpoint
from utilities.release import release
from utilities.trustyai import WORKLOAD_OPERATOR_DEPLOYMENT

LOGGER = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.evalhub,
    pytest.mark.usefixtures("evalhub_tenant_ca", "reconciler_test_ca"),
]

FAILURE_PHASE = "trustyai.opendatahub.io/evaluation-phase"
FAILURE_REPORTED = "trustyai.opendatahub.io/evalhub-failure-reported"


@pytest.fixture(scope="module")
def reconciler_test_ca(
    kube_context: str,
    namespace: str,
    core_client: client.CoreV1Api,
    apps_client: client.AppsV1Api,
    evalhub_instance: dict,
    evalhub_instance_namespace: str,
) -> Iterator[None]:
    """Trust the harness-generated EvalHub certificate for the operator's callbacks on Kind.

    On OpenShift the operator already trusts the service CA that signs the
    in-cluster certificate, so the patch is Kind-only. The module operator can
    reconcile this Deployment while the patch is in place: what preserves the
    patch is field ownership - the CA environment variable, volume and mount
    stay outside the fields the module operator's server-side apply owns.
    `evalhub_instance` is requested only for ordering: it is what creates the
    TLS secret `instance_tls_cert` reads.
    """
    if not kube_context.startswith("kind-"):
        yield
        return

    deployment = apps_client.read_namespaced_deployment(WORKLOAD_OPERATOR_DEPLOYMENT, namespace)
    manager = next(container for container in deployment.spec.template.spec.containers if container.name == "manager")
    env_name = "TRUSTYAI_EVALHUB_HTTP_EXTRA_CA_BUNDLE"
    previous = next((env for env in manager.env or [] if env.name == env_name), None)
    volume = f"evalhub-test-ca-{uuid4().hex[:8]}"
    mount_path = f"/var/run/{volume}"
    certificate = instance_tls_cert(core_client, evalhub_instance_namespace)
    core_client.create_namespaced_config_map(
        namespace,
        client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=volume),
            data={"ca.crt": certificate.decode()},
        ),
    )

    def patch(volume_patch: dict, mount_patch: dict, env_patch: dict) -> None:
        apps_client.patch_namespaced_deployment(
            name=WORKLOAD_OPERATOR_DEPLOYMENT,
            namespace=namespace,
            body={
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": [volume_patch],
                            "containers": [{"name": "manager", "volumeMounts": [mount_patch], "env": [env_patch]}],
                        }
                    }
                }
            },
            _content_type="application/strategic-merge-patch+json",
        )

    def wait_for_rollout(expected: str) -> None:
        wait_for_container_env(apps_client, WORKLOAD_OPERATOR_DEPLOYMENT, namespace, "manager", env_name, expected)

    try:
        patch(
            {"name": volume, "configMap": {"name": volume}},
            {"name": volume, "mountPath": mount_path, "readOnly": True},
            {"name": env_name, "value": f"{mount_path}/ca.crt", "valueFrom": None},
        )
        wait_for_rollout(f"{mount_path}/ca.crt")
        yield
    finally:
        restored_env = (
            {"name": env_name, "$patch": "delete"}
            if previous is None
            else {
                "name": env_name,
                "value": previous.value,
                "valueFrom": apps_client.api_client.sanitize_for_serialization(previous.value_from),
            }
        )
        patch(
            {"name": volume, "$patch": "delete"},
            {"mountPath": mount_path, "$patch": "delete"},
            restored_env,
        )
        wait_for_rollout((previous.value or "") if previous is not None else "")
        core_client.delete_namespaced_config_map(volume, namespace)


@pytest.mark.parametrize(
    ("image", "reason", "minimum_wait", "deadline"),
    [
        # The grace this case asserts is what trustyai-service-operator#1139
        # added and release 3.7 ships first: an operator without it reports
        # ImagePullBackOff immediately, so the case is gated to that release and
        # to its own section: it waits out most of the operator's ten minutes.
        pytest.param(
            f"registry.k8s.io/pause:evalhub-missing-{uuid4().hex}",
            "ImagePullBackOff",
            570,
            750,
            id="image-pull-grace",
            marks=[release(min="3.7"), pytest.mark.image_pull_grace],
        ),
        # Fatal waiting reasons are reported without a grace period on the
        # operators release 3.6 already ships: a regression check from there.
        pytest.param(
            "registry.k8s.io/pause:invalid/tag",
            "InvalidImageName",
            0,
            120,
            id="invalid-image-name",
            marks=release(min="3.6"),
        ),
    ],
)
def test_reconciler_reports_image_failure(
    core_client: client.CoreV1Api,
    evalhub_api_base: str,
    evalhub_tenant_token: str,
    evalhub_tenant_namespace: str,
    model_endpoint: ModelEndpoint,
    image: str,
    reason: str,
    minimum_wait: int,
    deadline: int,
) -> None:
    namespace = evalhub_tenant_namespace
    headers = api_headers(evalhub_tenant_token, namespace)
    provider = create_provider(
        evalhub_api_base,
        headers,
        {
            "name": f"image-failure-{uuid4().hex}",
            "title": "Image failure regression",
            "runtime": {"k8s": {"image": image, "entrypoint": ["/unused"]}},
            "benchmarks": [{"id": "image-failure", "name": "Image failure", "category": "test"}],
        },
    )
    provider_id = provider["resource"]["id"]
    job_id = None
    try:
        submission = submit_job(
            evalhub_api_base,
            headers,
            {
                "name": "image-failure-regression",
                "model": {"name": model_endpoint.name, "url": model_endpoint.url},
                "benchmarks": [{"id": "image-failure", "provider_id": provider_id}],
            },
        )
        job_id = submission["resource"]["id"]
        selector = f"job_id={job_id}"
        # Reaching the first pull failure can take minutes on a Kind node whose
        # path to the registry is slow, so the window to observe it is generous.
        observation_deadline = time.monotonic() + 300
        observed_states = set()
        while time.monotonic() < observation_deadline:
            pods = core_client.list_namespaced_pod(namespace, label_selector=selector).items
            for pod in pods:
                for container in (pod.status.init_container_statuses or []) + (pod.status.container_statuses or []):
                    waiting = container.state.waiting
                    if waiting:
                        state = f"{container.name}: {waiting.reason} ({waiting.message})"
                        if state not in observed_states:
                            LOGGER.info("evaluation %s: container state %s", job_id, state)
                            observed_states.add(state)
            if any(state.startswith(f"adapter: {reason} (") for state in observed_states):
                observed_at = time.monotonic()
                break
            status = read_job(evalhub_api_base, headers, job_id)["status"]
            assert status["state"] in ("pending", "running"), (
                f"evaluation {job_id} ended before the adapter entered {reason}: "
                f"{status}; observed container states: {sorted(observed_states)}"
            )
            time.sleep(WAIT_INTERVAL)
        else:
            pytest.fail(
                f"adapter for evaluation {job_id} never entered {reason}; "
                f"observed container states: {sorted(observed_states)}",
                pytrace=False,
            )

        # The operator's grace clock starts at the first BackOff Event, which can
        # precede this observation, so the elapsed time is asserted once the job
        # ends rather than polled inside the window: an early report still fails,
        # but a slow poll can no longer turn a healthy run into a failure.
        finished = wait_for_job(evalhub_api_base, headers, job_id, timeout=deadline, interval=WAIT_INTERVAL)
        elapsed = time.monotonic() - observed_at
        assert elapsed >= minimum_wait, (
            f"evaluation {job_id} failed {elapsed:.0f}s after the adapter entered {reason}, "
            f"short of the {minimum_wait}s grace period the operator must honor"
        )
        status = finished["status"]
        assert status["state"] == "failed", status
        benchmark = next(item for item in status["benchmarks"] if item["id"] == "image-failure")
        assert benchmark["status"] == "failed", benchmark
        assert reason in benchmark["error_message"]["message"], benchmark

        jobs_api = client.BatchV1Api(core_client.api_client)
        label_deadline = time.monotonic() + 30
        while time.monotonic() < label_deadline:
            jobs = jobs_api.list_namespaced_job(namespace, label_selector=selector).items
            if any(
                (job.metadata.labels or {}).get(FAILURE_PHASE) == "Failed"
                and (job.metadata.annotations or {}).get(FAILURE_REPORTED) == "true"
                for job in jobs
            ):
                break
            time.sleep(WAIT_INTERVAL)
        else:
            pytest.fail(
                f"operator did not promote the Kubernetes Job for evaluation {job_id} to Failed "
                "with its failure reported",
                pytrace=False,
            )
    finally:
        if keep_resources():
            LOGGER.warning(
                "KEEP_RESOURCES is set: leaving evaluation job %s with its pod and adapter logs, "
                "and provider %s, in namespace %s for inspection",
                job_id,
                provider_id,
                namespace,
            )
        else:
            try:
                if job_id:
                    delete_job(evalhub_api_base, headers, job_id)
            finally:
                delete_provider(evalhub_api_base, headers, provider_id)
