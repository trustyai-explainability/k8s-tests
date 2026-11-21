"""Checks for the EvalHub adapters: the section that runs real benchmarks.

Each job here hands one benchmark of the `lm_evaluation_harness` provider to its
adapter through the EvalHub API, against the LLM emulator the fixtures deploy or,
when `MODEL_URL` is set, a real endpoint, waits for the adapter to finish in the
cluster, and reads the result back. `BENCHMARKS` is what the section covers; a new
benchmark is one entry there, and every check below then runs once for it.

The section carries the `evalhub` and `adapters` markers: `evalhub` puts it in the
EvalHub selection, `adapters` names the section within it. An adapter run costs
minutes per benchmark - the adapter image is pulled and the dataset fetched from
the Hub - so it is never part of a default run: `k8s-tests test` excludes the
`adapters` marker, `--section evalhub` is `evalhub and not adapters`, and
`--section adapters` runs this section alone, into its own report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pytest

from utilities.emulator import auth_state
from utilities.evalhub import api_headers, benchmark_result, submit_job, wait_for_job
from utilities.model import ModelEndpoint

LOGGER = logging.getLogger(__name__)

# One real benchmark per job, through the same tenant RBAC and image pairing as the
# benchmark section: a pre-module operator needs an EvalHub service and adapter image from
# its own generation (see that module).
pytestmark = [pytest.mark.evalhub, pytest.mark.adapters]

LM_EVAL_PROVIDER = "lm_evaluation_harness"
# One example: enough for the harness to score, small enough for a Kind cluster.
DEFAULT_NUM_EXAMPLES = 1
# Longer than the harness smoke job's budget: a cold run pulls the adapter image
# and fetches the benchmark's dataset before the first request reaches the model.
JOB_TIMEOUT = 1800


@dataclass(frozen=True)
class Benchmark:
    """One benchmark a job submits, and the metric its provider scores it on."""

    id: str
    provider_id: str = LM_EVAL_PROVIDER
    # The provider config's `primary_score.metric`; None leaves the check to the
    # results the job reports.
    primary_metric: str | None = None
    num_examples: int = DEFAULT_NUM_EXAMPLES


# ethics_cm is the commonsense split of hendrycks/ethics in lm-evaluation-harness,
# which the provider config scores on `acc`.
BENCHMARKS: tuple[Benchmark, ...] = (Benchmark(id="ethics_cm", primary_metric="acc"),)


def job_payload(benchmark: Benchmark, model: ModelEndpoint, credentials_secret: str | None) -> dict[str, Any]:
    """The job body a user would submit for one benchmark against the model."""
    payload: dict[str, Any] = {
        "name": f"model-{benchmark.id}",
        "model": {
            "url": model.url,
            "name": model.name,
        },
        "benchmarks": [
            {
                "id": benchmark.id,
                "provider_id": benchmark.provider_id,
                "parameters": {"num_examples": benchmark.num_examples},
            }
        ],
    }
    if credentials_secret:
        payload["model"]["auth"] = {"secret_ref": credentials_secret}
    return payload


@pytest.fixture(scope="module", params=BENCHMARKS, ids=[benchmark.id for benchmark in BENCHMARKS])
def benchmark(request: pytest.FixtureRequest) -> Benchmark:
    """The benchmark under test; every check runs once per entry in BENCHMARKS."""
    return request.param


@pytest.fixture(scope="module")
def submitted(
    benchmark: Benchmark,
    evalhub_api_base: str,
    evalhub_tenant_token: str,
    evalhub_tenant_namespace: str,
    evalhub_model_credentials: str | None,
    model_endpoint: ModelEndpoint,
) -> dict[str, Any]:
    """Submit the benchmark's job, recording what the emulator had seen before it."""
    payload = job_payload(benchmark, model_endpoint, evalhub_model_credentials)
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


def test_the_job_is_accepted_with_the_requested_benchmark(submitted: dict[str, Any], benchmark: Benchmark) -> None:
    """The API takes the job and echoes back exactly the one benchmark asked for."""
    job = submitted["job"]
    assert job["resource"]["id"], f"the server returned no job id: {job}"
    assert job["status"]["state"] in ("pending", "running"), f"unexpected initial state: {job['status']['state']}"
    submitted_benchmarks = job["benchmarks"]
    assert len(submitted_benchmarks) == 1, (
        f"the job carries {len(submitted_benchmarks)} benchmarks: {submitted_benchmarks}"
    )
    entry = submitted_benchmarks[0]
    assert (entry["id"], entry["provider_id"]) == (benchmark.id, benchmark.provider_id)
    assert entry["parameters"]["num_examples"] == benchmark.num_examples


def test_the_job_completes(finished: dict[str, Any], benchmark: Benchmark) -> None:
    status = finished["status"]
    assert status["state"] == "completed", (
        f"job {finished['resource']['id']} ended {status['state']}: {(status.get('message') or {}).get('message')}"
    )
    benchmark_status = status["benchmarks"][0]
    assert benchmark_status["status"] == "completed", (
        f"{benchmark.id} ended {benchmark_status['status']}: {benchmark_status.get('error_message')}"
    )


def test_the_result_carries_the_benchmark_and_numeric_metrics(finished: dict[str, Any], benchmark: Benchmark) -> None:
    """`results.benchmarks[]` carries the benchmark, its provider and its metrics."""
    result = benchmark_result(finished, benchmark.id)
    LOGGER.info("%s result: %s", benchmark.id, result)
    assert result["provider_id"] == benchmark.provider_id
    assert result["benchmark_index"] == 0
    metrics = result.get("metrics")
    assert isinstance(metrics, dict) and metrics, f"{benchmark.id} completed without metrics: {result}"
    for name, value in metrics.items():
        assert isinstance(value, (int, float)), f"metric {name!r} is {value!r}, not a number"


def test_the_result_scores_the_primary_metric(finished: dict[str, Any], benchmark: Benchmark) -> None:
    """The metric the provider config scores the benchmark on is among those reported."""
    if benchmark.primary_metric is None:
        pytest.skip(f"{benchmark.id} names no primary metric in the table above")
    result = benchmark_result(finished, benchmark.id)
    metrics = result["metrics"]
    assert benchmark.primary_metric in metrics, (
        f"{benchmark.id} reports {sorted(metrics)}, not the {benchmark.primary_metric!r} its provider scores it on"
    )
    score = metrics[benchmark.primary_metric]
    assert 0.0 <= score <= 1.0, f"{benchmark.id} scored {score!r} on {benchmark.primary_metric}, which is not a rate"


def test_the_result_reports_the_provider_test_outcome(finished: dict[str, Any], benchmark: Benchmark) -> None:
    """The pass criteria in the provider config turn the score into a test result."""
    result = benchmark_result(finished, benchmark.id)
    test = result.get("test")
    if test is None:
        pytest.skip(f"{benchmark.id} reported no pass criteria in this deployment: {sorted(result)}")
    assert isinstance(test["primary_score"], (int, float))
    assert isinstance(test["pass"], bool)
    metric = test.get("primary_score_metric")
    if metric:
        assert metric in result["metrics"], (
            f"the test scored {metric!r}, which is not among the reported metrics {sorted(result['metrics'])}"
        )


def test_the_job_reached_the_model(
    finished: dict[str, Any],
    submitted: dict[str, Any],
    model_endpoint: ModelEndpoint,
) -> None:
    """The emulator authenticated the job's model requests, so the job did the work.

    A job can reach `completed` without the model being called at all (a cached
    result, a misconfigured endpoint that answers anyway), so this reads the
    emulator's own count instead of trusting the job's verdict. A real endpoint
    does not report what it authenticated, so the check skips rather than
    pretending the job's own verdict proves the model was called.
    """
    if not model_endpoint.observes_requests or not model_endpoint.state_url:
        pytest.skip(f"MODEL_URL is set ({model_endpoint.url}); a real endpoint does not report the requests it served")
    state = auth_state(model_endpoint.state_url)
    before = submitted["auth_before"]
    assert state["authenticated_requests"] > before["authenticated_requests"], (
        f"job {finished['resource']['id']} reported {finished['status']['state']} but the emulator authenticated "
        f"no model request; before={before}, after={state}"
    )
    assert state["last_authenticated_path"].startswith("/v1/"), (
        f"the last authenticated request was {state['last_authenticated_path']!r}, not a model endpoint"
    )
