"""ATIF traces and job plumbing for the adapter checks.

The ATIF adapter scores *pre-recorded* trajectories, so these checks generate them:
a NeMo Gym export is a directory of ATIF documents plus a `manifest.jsonl` whose rows
carry `trajectory_path`, `_ng_task_index`, `_ng_rollout_index` and `expected_sha256`.
The documents are built here, published onto a PersistentVolumeClaim, and handed to
EvalHub as a job's `test_data_ref`, which mounts them at `/test_data` in the adapter.

The variants the checks need are the ones RHAI-22310 changed the adapter's handling
of: an export the manifest drives (joining and accounting), a digest the producer
gets wrong, an empty manifest, an ATIF-v1.6 document, an ATIF-v1.9 document, and a
root field the ATIF model does not declare.

Scoring needs an OpenAI-compatible judge that answers with the JSON the adapter
parses, which a vLLM emulator cannot do, so `deploy_judge` runs a small stub that
answers `{"criteria": [...]}` when asked for criteria and `{"score": ...}` otherwise.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from utilities.kube import wait_for_deployment_available

LOGGER = logging.getLogger(__name__)

PROVIDER_ID = "atif"
BENCHMARK_ID = "atif_trajectory_default"
PRIMARY_METRIC = "atif_overall_score"
MANIFEST_FILE = "manifest.jsonl"
ALPHA = "abcdefghijklmnopqrstuvwxyz0123456789"

# ATIF-v1.6 predates `session_id`/`trajectory_id` being optional and v1.9 does not exist
# in the SDK's Literal: one proves backward compatibility, the other the named range.
V16 = "ATIF-v1.6"
V17 = "ATIF-v1.7"
V19 = "ATIF-v1.9"

# A stub judge: deterministic, and never a real model, so a score is a fact about the
# adapter rather than about an endpoint's mood.
JUDGE_NAME = "atif-judge"
JUDGE_PORT = 8080
JUDGE_IMAGE = "docker.io/library/python:3.12-slim"
JUDGE_MODEL_NAME = "atif-judge"
DEPLOY_TIMEOUT = 300
LOADER_IMAGE = "docker.io/library/busybox:1.36"
LOADER_TIMEOUT = 300
DEFAULT_JOB_TIMEOUT = 1800
EXPORT_SUBPATH = "export"

JUDGE_SOURCE = '''
"""An OpenAI-compatible judge that answers the JSON the ATIF adapter parses."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def prompt_of(payload):
    """The adapter puts its JSON request in the user message of a chat completion."""
    for message in payload.get("messages") or []:
        if message.get("role") == "user":
            try:
                return json.loads(message.get("content") or "{}")
            except (TypeError, json.JSONDecodeError):
                return {}
    return payload


def answer(payload):
    """Criteria derivation, failure categorization, or one step's score."""
    request = prompt_of(payload)
    if isinstance(request.get("allowed_categories"), list):
        return {"category": "none", "confidence": 1.0, "rationale": "stub judge"}
    if "context" in request:
        return {"score": float(os.environ.get("JUDGE_SCORE", "0.75")), "rationale": "stub judge"}
    return {"criteria": [{"name": "answer quality", "description": "stub", "weight": 1.0}]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path in ("/health", "/v1/models"):
            self._send(200, {"data": [{"id": os.environ.get("JUDGE_MODEL", "atif-judge")}]})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid JSON"})
            return
        content = json.dumps(answer(payload))
        self._send(
            200,
            {
                "id": "stub",
                "object": "chat.completion",
                "model": os.environ.get("JUDGE_MODEL", "atif-judge"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def _send(self, status, body):
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        """Silence the default per-request log line."""


ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
'''


@dataclass(frozen=True)
class Export:
    """One published export: where the adapter finds it and how many rows it has."""

    claim_name: str
    sub_path: str
    entries: int


def judge_url(namespace: str) -> str:
    return f"http://{JUDGE_NAME}.{namespace}.svc.cluster.local:{JUDGE_PORT}/v1"


def _report(step_id: int, source: str, message: str, **extra: Any) -> dict[str, Any]:
    step: dict[str, Any] = {"step_id": step_id, "source": source, "message": message, **extra}
    return step


def trajectory(
    *,
    schema_version: str = V17,
    trajectory_id: str,
    task: str,
    score_hint: str = "answer",
    undeclared_field: str | None = None,
) -> dict[str, Any]:
    """One ATIF document, shaped like a NeMo Gym export.

    The task instruction lives in the declared `extra` object, which is where the
    adapter and the SDK expect producer metadata; `undeclared_field` puts one at the
    root instead, which the SDK rejects by design.
    """
    document: dict[str, Any] = {
        "schema_version": schema_version,
        "trajectory_id": trajectory_id,
        "agent": {"name": "gym", "version": "1.7.0"},
        "steps": [
            _report(1, "user", task),
            _report(2, "agent", f"working on {score_hint}"),
            _report(3, "agent", f"finished {score_hint}"),
        ],
        "extra": {"task_instruction": task, "producer_sha256": f"sha256-of-{trajectory_id}"},
    }
    if schema_version == V16:
        # The root session id became optional in ATIF-v1.7; v1.6 requires it, so a
        # document without one is not a valid v1.6 input for the range check to score.
        document["session_id"] = f"run-{trajectory_id}"
    if undeclared_field:
        document[undeclared_field] = "declared nowhere"
    return document


def encoded(document: dict[str, Any]) -> bytes:
    """The exact bytes a producer writes, which its manifest's digest covers."""
    return (json.dumps(document, indent=2) + "\n").encode()


def manifest_row(name: str, task_index: int, rollout_index: int, payload: bytes | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "trajectory_path": name,
        "_ng_task_index": task_index,
        "_ng_rollout_index": rollout_index,
    }
    if payload is not None:
        row["expected_sha256"] = hashlib.sha256(payload).hexdigest()
    return row


def export_files(
    *,
    schema_version: str = V17,
    entries: int = 2,
    digest_mismatch: bool | int = False,
    empty_manifest: bool = False,
    undeclared_field: str | None = None,
    unlisted: bool = True,
) -> dict[str, bytes]:
    """A NeMo Gym export: the manifest, the documents it lists, and one it does not.

    `digest_mismatch` picks which entries declare a digest their bytes do not have:
    `True` for all of them, an index for one, so a run can mix a verifiable entry with
    a wrong one.
    """
    files: dict[str, bytes] = {}
    rows: list[dict[str, Any]] = []
    if digest_mismatch is True:
        wrong_digest = set(range(entries))
    elif digest_mismatch is False:
        # `bool` is an `int`, so the two flags have to be handled before the index case.
        wrong_digest = set()
    else:
        wrong_digest = {int(digest_mismatch)}
    for index in range(entries):
        name = f"0-{index}.json"
        payload = encoded(
            trajectory(
                schema_version=schema_version,
                trajectory_id=f"t{index}",
                task=f"Summarise record {index}.",
                score_hint=f"record {index}",
                undeclared_field=undeclared_field,
            )
        )
        files[name] = payload
        row = manifest_row(name, 0, index, None if index in wrong_digest else payload)
        if index in wrong_digest:
            row["expected_sha256"] = "0" * 64
        rows.append(row)
    if unlisted:
        # Authoritative discovery must not load this one: it is not even JSON.
        files["unlisted.json"] = b"not an ATIF trajectory\n"
    body = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    files[MANIFEST_FILE] = body.encode()
    if empty_manifest:
        files[MANIFEST_FILE] = b""
    return files


def _create_or_replace_config_map(
    core_client: client.CoreV1Api, namespace: str, name: str, data: dict[str, str]
) -> None:
    body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        data=data,
    )
    try:
        core_client.create_namespaced_config_map(namespace=namespace, body=body)
    except ApiException as exc:
        if exc.status != 409:
            raise
        core_client.replace_namespaced_config_map(name=name, namespace=namespace, body=body)


def _claim(name: str, namespace: str) -> client.V1PersistentVolumeClaim:
    return client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            # A plain mapping: see utilities.mlflow._claim.
            resources={"requests": {"storage": "64Mi"}},
        ),
    )


def publish(
    core_client: client.CoreV1Api,
    batch_client: client.BatchV1Api,
    namespace: str,
    name: str,
    files: dict[str, bytes],
    entries: int | None = None,
) -> Export:
    """Write an export onto a claim, so a job can mount it as its `test_data_ref`.

    A ConfigMap carries the bytes and a one-shot Pod copies them onto the claim: the
    adapter mounts the claim read-only at `/test_data`, and the platform owns that
    mount, so the export has to exist before the job does.
    """
    LOGGER.info("publishing ATIF export %r (%d files) in namespace %s", name, len(files), namespace)
    _create_or_replace_config_map(
        core_client,
        namespace,
        f"{name}-files",
        {file_name: payload.decode() for file_name, payload in files.items()},
    )
    claim = _claim(name, namespace)
    try:
        core_client.create_namespaced_persistent_volume_claim(namespace=namespace, body=claim)
    except ApiException as exc:
        if exc.status != 409:
            raise

    job = client.V1Job(
        metadata=client.V1ObjectMeta(name=f"{name}-loader", namespace=namespace),
        spec=client.V1JobSpec(
            backoff_limit=0,
            ttl_seconds_after_finished=600,
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    restart_policy="Never",
                    containers=[
                        client.V1Container(
                            name="loader",
                            image=LOADER_IMAGE,
                            command=[
                                "sh",
                                "-c",
                                f"mkdir -p /data/{EXPORT_SUBPATH} && cp /files/* /data/{EXPORT_SUBPATH}/",
                            ],
                            volume_mounts=[
                                client.V1VolumeMount(name="data", mount_path="/data"),
                                client.V1VolumeMount(name="files", mount_path="/files", read_only=True),
                            ],
                            # busybox runs as root and the claim is mounted unowned;
                            # it only copies files and drops capabilities.
                            security_context=client.V1SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=client.V1Capabilities(drop=["ALL"]),
                            ),
                        )
                    ],
                    volumes=[
                        client.V1Volume(
                            name="data",
                            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=name),
                        ),
                        client.V1Volume(
                            name="files",
                            config_map=client.V1ConfigMapVolumeSource(name=f"{name}-files"),
                        ),
                    ],
                )
            ),
        ),
    )
    try:
        batch_client.create_namespaced_job(namespace=namespace, body=job)
    except ApiException as exc:
        if exc.status != 409:
            raise
    _wait_for_job(batch_client, f"{name}-loader", namespace)
    return Export(
        claim_name=name,
        sub_path=EXPORT_SUBPATH,
        # A scan-discovered export ships no manifest, so the caller counts its files.
        entries=entries if entries is not None else _manifest_entries(files),
    )


def _manifest_entries(files: dict[str, bytes]) -> int:
    raw = files.get(MANIFEST_FILE, b"").decode().strip()
    return len([line for line in raw.splitlines() if line.strip()])


def _wait_for_job(batch_client: client.BatchV1Api, name: str, namespace: str, timeout: int = LOADER_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while True:
        job = batch_client.read_namespaced_job(name=name, namespace=namespace)
        status = job.status
        if status.succeeded:
            return
        conditions = status.conditions or []
        failed = [condition for condition in conditions if condition.type == "Failed" and condition.status == "True"]
        if failed:
            pytest.fail(f"job {namespace}/{name} failed: {failed[0].message}", pytrace=False)
        if time.monotonic() >= deadline:
            pytest.fail(f"job {namespace}/{name} did not finish within {timeout}s", pytrace=False)
        time.sleep(2)


def deploy_judge(
    core_client: client.CoreV1Api,
    apps_client: client.AppsV1Api,
    namespace: str,
    score: float = 0.75,
) -> str:
    """Deploy the stub judge and return its OpenAI-compatible base URL."""
    LOGGER.info("deploying the ATIF judge stub in namespace %s (score=%s)", namespace, score)
    _create_or_replace_config_map(core_client, namespace, f"{JUDGE_NAME}-source", {"judge.py": JUDGE_SOURCE})
    labels = {"app.kubernetes.io/name": JUDGE_NAME}
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(name=JUDGE_NAME, namespace=namespace, labels=labels),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels=labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name=JUDGE_NAME,
                            image=JUDGE_IMAGE,
                            command=["python", "/src/judge.py"],
                            env=[
                                client.V1EnvVar(name="PORT", value=str(JUDGE_PORT)),
                                client.V1EnvVar(name="JUDGE_SCORE", value=str(score)),
                                client.V1EnvVar(name="JUDGE_MODEL", value=JUDGE_MODEL_NAME),
                            ],
                            ports=[client.V1ContainerPort(name="http", container_port=JUDGE_PORT, protocol="TCP")],
                            readiness_probe=client.V1Probe(
                                http_get=client.V1HTTPGetAction(path="/health", port=JUDGE_PORT),
                                initial_delay_seconds=2,
                                period_seconds=5,
                                timeout_seconds=3,
                                failure_threshold=12,
                            ),
                            volume_mounts=[
                                client.V1VolumeMount(name="source", mount_path="/src", read_only=True),
                            ],
                            security_context=client.V1SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=client.V1Capabilities(drop=["ALL"]),
                            ),
                        )
                    ],
                    volumes=[
                        client.V1Volume(
                            name="source",
                            config_map=client.V1ConfigMapVolumeSource(name=f"{JUDGE_NAME}-source"),
                        )
                    ],
                ),
            ),
        ),
    )
    try:
        apps_client.create_namespaced_deployment(namespace=namespace, body=deployment)
    except ApiException as exc:
        if exc.status != 409:
            raise
        apps_client.replace_namespaced_deployment(name=JUDGE_NAME, namespace=namespace, body=deployment)
    service = client.V1Service(
        metadata=client.V1ObjectMeta(name=JUDGE_NAME, namespace=namespace, labels=labels),
        spec=client.V1ServiceSpec(
            selector=labels,
            ports=[client.V1ServicePort(name="http", port=JUDGE_PORT, target_port=JUDGE_PORT, protocol="TCP")],
        ),
    )
    try:
        core_client.create_namespaced_service(namespace=namespace, body=service)
    except ApiException as exc:
        if exc.status != 409:
            raise
        core_client.replace_namespaced_service(name=JUDGE_NAME, namespace=namespace, body=service)
    wait_for_deployment_available(apps_client, JUDGE_NAME, namespace, timeout=DEPLOY_TIMEOUT, core_client=core_client)
    return judge_url(namespace)


def job_payload(
    *,
    name: str,
    export: Export,
    judge_base_url: str,
    experiment: str,
    parameters: dict[str, Any] | None = None,
    credentials_secret: str | None = None,
) -> dict[str, Any]:
    """The body a user submits for one ATIF export.

    `experiment.name` is what makes EvalHub resolve an MLflow experiment, which is how
    the adapter gets the `experiment_name` it requires before writing artifacts.
    """
    job_parameters: dict[str, Any] = {
        "scoring_mode": "auto",
        "partial_result_policy": "fail_fast",
        "invalid_input_policy": "skip_invalid",
    }
    job_parameters.update(parameters or {})
    model: dict[str, Any] = {"url": judge_base_url, "name": JUDGE_MODEL_NAME}
    if credentials_secret:
        model["auth"] = {"secret_ref": credentials_secret}
    return {
        "name": name,
        "experiment": {"name": experiment},
        "model": model,
        "benchmarks": [
            {
                "id": BENCHMARK_ID,
                "provider_id": PROVIDER_ID,
                "parameters": job_parameters,
                "test_data_ref": {
                    "type": "pre_recorded_data",
                    "pvc": {"claim_name": export.claim_name, "sub_path": export.sub_path},
                },
            }
        ],
    }
