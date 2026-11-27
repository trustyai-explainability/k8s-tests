"""Check the operator's post-processor image reaches a standalone Kubernetes Job."""

from __future__ import annotations

import os
import time

import pytest
from kubernetes import client

from utilities.evalhub import EVALHUB_CONTAINER_NAME, EVALHUB_NAME, api_headers, request
from utilities.kube import read_deployment

EXPECTED_IMAGE = os.environ.get("EVALHUB_TEST_POST_PROCESSOR_IMAGE", "").strip()
POST_PROCESSING_PATH = "/api/v1/evaluations/post-processing"
pytestmark = [
    pytest.mark.evalhub,
    pytest.mark.skipif(not EXPECTED_IMAGE, reason="set EVALHUB_TEST_POST_PROCESSOR_IMAGE to opt in"),
]


def test_post_processing_job_uses_the_injected_adapter_image(
    apps_client: client.AppsV1Api,
    core_client: client.CoreV1Api,
    evalhub_api_base: str,
    evalhub_instance_namespace: str,
    evalhub_tenant_namespace: str,
    evalhub_tenant_token: str,
    evalhub_tenant_ca: None,
) -> None:
    deployment = read_deployment(apps_client, name=EVALHUB_NAME, namespace=evalhub_instance_namespace)
    server = next(c for c in deployment.spec.template.spec.containers if c.name == EVALHUB_CONTAINER_NAME)
    injected = [env.value for env in server.env or [] if env.name == "EVALHUB_POST_PROCESSING_IMAGE"]
    assert injected == [EXPECTED_IMAGE], (
        f"expected {EXPECTED_IMAGE!r} from operator params.env in the EvalHub Deployment, got {injected}"
    )

    # The PVC is intentionally not provisioned: this test inspects the Job template,
    # not scheduling, image pulls, or confidence-interval computation.
    pvc = {"claim_name": "post-processing-image-test-data"}
    payload = {
        "name": "post-processing-image-check",
        "operations": {
            "confidence_interval": {
                "significance_level": 0.05,
                "primary_score": {"metric": "accuracy"},
                "results_data_ref": {
                    "pvc": pvc,
                    "data_config": {"format": "jsonl", "columns": {"prediction": "prediction"}},
                },
                "calibration_data_ref": [
                    {"pvc": pvc, "data_config": {"format": "jsonl", "columns": {"label": "y", "prediction": "p"}}}
                ],
            }
        },
    }
    headers = api_headers(evalhub_tenant_token, evalhub_tenant_namespace)
    url = f"{evalhub_api_base}{POST_PROCESSING_PATH}"
    response = request("post", url, headers=headers, json_body=payload)
    assert response.status_code == 202, f"post-processing submission failed: {response.status_code} {response.text}"
    job_id = response.json()["resource"]["id"]
    resource_url = f"{url}/{job_id}"
    jobs_api = client.BatchV1Api(core_client.api_client)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            jobs = jobs_api.list_namespaced_job(evalhub_tenant_namespace, label_selector=f"job_id={job_id}").items
            if jobs:
                for job in jobs:
                    images = [c.image for c in job.spec.template.spec.containers if c.name == "adapter"]
                    assert images == [EXPECTED_IMAGE], f"{job.metadata.name}: expected {EXPECTED_IMAGE!r}, got {images}"
                return
            status = request("get", resource_url, headers=headers)
            assert status.status_code == 200, status.text
            assert status.json()["status"]["state"] not in ("failed", "cancelled"), status.text
            time.sleep(2)
        pytest.fail(f"post-processing evaluation {job_id} did not create a Kubernetes Job")
    finally:
        deleted = request("delete", resource_url, headers=headers)
        assert deleted.status_code == 204, f"could not delete post-processing evaluation {job_id}: {deleted.text}"
