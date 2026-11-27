"""Checks for an evaluation job that creates a benchmark against the model.

The job is the smallest real one the harness can run: one benchmark, one dataset
example, against the LLM emulator the fixtures deploy or, when `MODEL_URL` is
set, a real endpoint. It exercises the whole path an adapter takes - provider,
model URL, model credential - and the emulator is strict about the credential, so
the checks can prove the token in the tenant Secret reached the model rather than
the run merely finishing.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

import pytest
import yaml
from kubernetes import client

from utilities.emulator import auth_state
from utilities.evalhub import EVALHUB_NAME, api_headers, benchmark_result, read_job, submit_job, wait_for_job
from utilities.model import ModelEndpoint

LOGGER = logging.getLogger(__name__)

# Nothing here is release-specific: what a pre-module cluster needs is an EvalHub service
# and adapter image from its own generation. The 3.5 operator pins `evalhub:latest` and
# `ta-lmes-job:latest`, both moving tags whose current content no longer pairs with its
# RBAC (a 1.0 service creates a NetworkPolicy per job, eval-hub #1070, which 3.5 does not
# allow) or with its job contract. The GA pair `odh-eval-hub:odh-3.5` and
# `ta-lmes-job:odh-3.5-ga` does, and `deploy` warns when a single-layout run leaves both
# at the operator's pins.
pytestmark = [pytest.mark.evalhub, pytest.mark.benchmark]

BENCHMARK_ID = "arc_easy"
PROVIDER_ID = "lm_evaluation_harness"
# One example: enough to score, small enough to run in a Kind cluster.
NUM_EXAMPLES = 1
JOB_TIMEOUT = 900


def job_payload(model: ModelEndpoint, credentials_secret: str | None) -> dict[str, Any]:
    """The job body a user would submit for one benchmark against the model."""
    payload: dict[str, Any] = {
        "name": f"model-{BENCHMARK_ID}",
        "model": {
            "url": model.url,
            "name": model.name,
        },
        "benchmarks": [
            {
                "id": BENCHMARK_ID,
                "provider_id": PROVIDER_ID,
                "parameters": {"num_examples": NUM_EXAMPLES},
            }
        ],
    }
    if credentials_secret:
        payload["model"]["auth"] = {"secret_ref": credentials_secret}
    return payload


@pytest.fixture(scope="module")
def submitted(
    evalhub_api_base: str,
    evalhub_tenant_token: str,
    evalhub_tenant_namespace: str,
    evalhub_model_credentials: str | None,
    model_endpoint: ModelEndpoint,
) -> dict[str, Any]:
    """Submit the job, recording what the emulator had seen before it."""
    payload = job_payload(model_endpoint, evalhub_model_credentials)
    headers = api_headers(evalhub_tenant_token, evalhub_tenant_namespace)
    before = auth_state(model_endpoint.state_url) if model_endpoint.state_url else None
    job = submit_job(evalhub_api_base, headers, payload)
    return {"job": job, "headers": headers, "payload": payload, "auth_before": before}


@pytest.fixture(scope="module")
def finished(submitted: dict[str, Any], evalhub_api_base: str) -> dict[str, Any]:
    """Wait for the job to reach a terminal state."""
    return wait_for_job(
        evalhub_api_base,
        submitted["headers"],
        submitted["job"]["resource"]["id"],
        timeout=JOB_TIMEOUT,
    )


def test_the_job_is_accepted_with_the_requested_benchmark(submitted: dict[str, Any]) -> None:
    job = submitted["job"]
    assert job["resource"]["id"], f"the server returned no job id: {job}"
    assert job["status"]["state"] in ("pending", "running"), f"unexpected initial state: {job['status']['state']}"
    benchmark = job["benchmarks"][0]
    assert (benchmark["id"], benchmark["provider_id"]) == (BENCHMARK_ID, PROVIDER_ID)
    assert benchmark["parameters"]["num_examples"] == NUM_EXAMPLES


def test_the_job_uses_the_injected_sidecar_image(
    apps_client: client.AppsV1Api,
    core_client: client.CoreV1Api,
    evalhub_api_base: str,
    evalhub_instance_namespace: str,
    evalhub_tenant_namespace: str,
    submitted: dict[str, Any],
) -> None:
    """The benchmark Job must use the image injected in the running EvalHub's config."""
    deployment = apps_client.read_namespaced_deployment(EVALHUB_NAME, evalhub_instance_namespace)
    config_volume = next(volume for volume in deployment.spec.template.spec.volumes if volume.name == "evalhub-config")
    config_map = core_client.read_namespaced_config_map(config_volume.config_map.name, evalhub_instance_namespace)
    service_config = yaml.safe_load(config_map.data["config.yaml"])
    expected = service_config.get("sidecar", {}).get("sidecar_container", {}).get("image", "").strip()
    assert expected and expected != "eval-runtime-sidecar:latest", "operator did not inject a sidecar image"

    job_id = submitted["job"]["resource"]["id"]
    jobs_api = client.BatchV1Api(core_client.api_client)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        jobs = jobs_api.list_namespaced_job(evalhub_tenant_namespace, label_selector=f"job_id={job_id}").items
        if jobs:
            for job in jobs:
                pod_spec = job.spec.template.spec
                sidecars = [
                    container.image
                    for container in (pod_spec.init_containers or []) + (pod_spec.containers or [])
                    if container.name == "sidecar"
                ]
                assert sidecars == [expected], f"{job.metadata.name}: expected sidecar {expected!r}, got {sidecars}"
            return
        status = read_job(evalhub_api_base, submitted["headers"], job_id)["status"]
        assert status["state"] not in ("failed", "cancelled"), (
            f"evaluation {job_id} ended before Job creation: {status}"
        )
        time.sleep(2)
    pytest.fail(f"evaluation {job_id} did not create a Kubernetes Job in {evalhub_tenant_namespace}")


def test_the_job_completes(finished: dict[str, Any]) -> None:
    status = finished["status"]
    assert status["state"] == "completed", (
        f"job {finished['resource']['id']} ended {status['state']}: {(status.get('message') or {}).get('message')}"
    )
    benchmark_status = status["benchmarks"][0]
    assert benchmark_status["status"] == "completed", (
        f"{BENCHMARK_ID} ended {benchmark_status['status']}: {benchmark_status.get('error_message')}"
    )


def test_the_result_has_the_expected_shape(finished: dict[str, Any]) -> None:
    """`results.benchmarks[]` carries the benchmark, its provider and its metrics."""
    result = benchmark_result(finished, BENCHMARK_ID)
    LOGGER.info("benchmark result: %s", result)
    assert result["provider_id"] == PROVIDER_ID
    assert result["benchmark_index"] == 0
    assert isinstance(result.get("metrics"), dict) and result["metrics"], (
        f"{BENCHMARK_ID} completed without metrics: {result}"
    )
    for name, value in result["metrics"].items():
        assert isinstance(value, (int, float)), f"metric {name!r} is {value!r}, not a number"


def test_the_benchmark_reports_a_test_outcome(finished: dict[str, Any]) -> None:
    """The pass criteria in the provider config turn the score into a test result."""
    result = benchmark_result(finished, BENCHMARK_ID)
    test = result.get("test")
    if test is None:
        pytest.skip(f"{BENCHMARK_ID} reported no pass criteria in this deployment: {sorted(result)}")
    assert isinstance(test["primary_score"], (int, float))
    assert isinstance(test["pass"], bool)
    metric = test.get("primary_score_metric")
    if metric:
        assert metric in result["metrics"], (
            f"the test scored {metric!r}, which is not among the reported metrics {sorted(result['metrics'])}"
        )


def test_the_token_reached_the_model(
    finished: dict[str, Any],
    submitted: dict[str, Any],
    model_endpoint: ModelEndpoint,
) -> None:
    """The emulator saw the tenant Secret's token on the job's model requests.

    This is the point of the credential plumbing: the job's `model.auth.secret_ref`
    resolves to `api-key`, the sidecar injects it, and the emulator rejects
    anything else - so an authenticated request can only come from that token.
    A real endpoint does not report what it authenticated, so the check skips
    rather than pretending the job's own verdict proves the credential arrived.
    """
    if not model_endpoint.observes_requests or not model_endpoint.state_url:
        pytest.skip(f"MODEL_URL is set ({model_endpoint.url}); a real endpoint does not report the requests it served")
    state = auth_state(model_endpoint.state_url)
    before = submitted["auth_before"]
    assert state["authenticated_requests"] > before["authenticated_requests"], (
        f"the job completed but the emulator authenticated no model request; before={before}, after={state}"
    )
    assert state["last_authenticated_path"].startswith("/v1/"), (
        f"the last authenticated request was {state['last_authenticated_path']!r}, not a model endpoint"
    )
    assert model_endpoint.credential, "the emulator endpoint has no credential to compare"
    fingerprint = hashlib.sha256(model_endpoint.credential.encode()).hexdigest()[:16]
    assert fingerprint in state["presented_fingerprints"], (
        "the emulator did not see the tenant Secret's token; it saw "
        f"{state['presented_fingerprints']} instead of {fingerprint}"
    )
