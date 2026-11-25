"""The producer shapes the ATIF checks validate, and the exports they write.

The adapter is a cross-producer surface: a trajectory recorded by one agent harness
has to score the same way as the equivalent trajectory recorded by another. A
producer differs from the next in three ways the adapter has to absorb, and the
committed matrix (`tests/evalhub/fixtures/atif_producer_matrix.md`) names, per
producer and per difference class, the check that covers it:

- **joining and naming**: NeMo Gym ships a `manifest.jsonl` that is authoritative
  about which files are evaluated and in what order; Harbor, OpenHands and Claude
  Code ship a directory the adapter discovers itself.
- **provenance**: NeMo Gym declares `expected_sha256` in its manifest rows and keeps
  its own metadata in ATIF's declared `extra` object; the other producers declare no
  digest, so the adapter records only the hash it computed from the bytes it read.
- **field handling**: Harbor records token metrics and multimodal content parts;
  NeMo Gym rejects both at ingestion, so its documents carry neither. The adapter
  projects a step down to its text before judging (`scoring_context.project_step`),
  so the difference must not move the score.

Only producers whose export shape can be checked against a real artefact are built
here. NeMo Gym follows `gym eval export atif` (flat `0-<rollout>.json` files plus a
manifest carrying `trajectory_path`, `_ng_task_index`, `_ng_rollout_index` and
`expected_sha256`). Harbor follows the `harbor-timeout.trajectory.json` fixture the
adapter itself ships: an ATIF-v1.6 document named `*.trajectory.json`, with step
metrics and a content-part message. OpenHands, Claude Code and Training Hub have no
fixture in any repository yet, so their matrix rows are recorded unvalidated rather
than invented here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from utilities import atif


@dataclass(frozen=True)
class Producer:
    """One agent harness, and how its export differs from the others."""

    name: str
    #: Whether the export carries a `manifest.jsonl` that decides what is evaluated.
    manifest: bool
    #: The file name the producer writes for rollout `index`.
    file_pattern: str
    #: Whether the manifest declares a digest of each file's bytes.
    digests: bool
    #: Whether a document carries token metrics and multimodal content parts.
    rich_fields: bool
    #: The ATIF version the producer emits.
    schema_version: str
    #: Whether a check in this repository covers the producer.
    validated: bool
    #: What the provenance of its export is, for the matrix.
    provenance: str


NEMO_GYM = Producer(
    name="NeMo Gym",
    manifest=True,
    file_pattern="0-{index}.json",
    digests=True,
    rich_fields=False,
    schema_version=atif.V17,
    validated=True,
    provenance="manifest `expected_sha256`, producer metadata in `extra`",
)

HARBOR = Producer(
    name="Harbor",
    manifest=False,
    file_pattern="{index}.trajectory.json",
    digests=False,
    rich_fields=True,
    schema_version=atif.V16,
    validated=True,
    provenance="no digest; the adapter hashes the bytes it reads",
)

# Recorded as unvalidated until a real export from each is available: guessing a
# producer's file naming would test the guess, not the adapter.
OPENHANDS = Producer(
    name="OpenHands",
    manifest=False,
    file_pattern="{index}.atif.json",
    digests=False,
    rich_fields=False,
    schema_version=atif.V17,
    validated=False,
    provenance="no fixture in any repository yet",
)

CLAUDE_CODE = Producer(
    name="Claude Code",
    manifest=False,
    file_pattern="{index}.atif.json",
    digests=False,
    rich_fields=False,
    schema_version=atif.V17,
    validated=False,
    provenance="no fixture in any repository yet",
)

TRAINING_HUB = Producer(
    name="Training Hub",
    manifest=True,
    file_pattern="0-{index}.json",
    digests=False,
    rich_fields=False,
    schema_version=atif.V17,
    validated=False,
    provenance="ATIF-v1.7 writer still in progress (RHAISTRAT-2815)",
)

PRODUCERS = (HARBOR, NEMO_GYM, OPENHANDS, CLAUDE_CODE, TRAINING_HUB)


def _harbor_document(trajectory_id: str, task: str, *, rich_fields: bool) -> dict[str, Any]:
    """One ATIF-v1.6 document shaped like the Harbor fixture the adapter ships.

    A v1.6 document needs a session id, and Harbor's agent is `terminus-2` with a
    model name per step. `rich_fields` adds what Harbor records and NeMo Gym
    refuses: token metrics on the step, and a content-part message rather than a
    plain string.
    """
    message: str | list[dict[str, Any]] = f"working on {task}"
    if rich_fields:
        message = [
            {"type": "text", "text": f"working on {task}"},
            {"type": "image_url", "image_url": {"url": "file:///tmp/frame-1.png"}},
        ]
    step: dict[str, Any] = {
        "step_id": 2,
        "source": "agent",
        "model_name": "openai/gpt-4o",
        "message": message,
        "tool_calls": [
            {
                "tool_call_id": "call_0_1",
                "function_name": "bash_command",
                "arguments": {"keystrokes": "ls -la\n", "duration": 0.1},
            }
        ],
        "observation": {"results": [{"content": "total 0\n"}]},
    }
    if rich_fields:
        step["metrics"] = {"prompt_tokens": 1200, "completion_tokens": 64, "cached_tokens": 0}
    return {
        "schema_version": atif.V16,
        "session_id": f"run-{trajectory_id}",
        "trajectory_id": trajectory_id,
        "agent": {"name": "terminus-2", "version": "2.0.0", "model_name": "openai/gpt-4o"},
        "steps": [
            {"step_id": 1, "source": "user", "message": task},
            step,
            {"step_id": 3, "source": "agent", "message": f"finished {task}"},
        ],
    }


def _nemo_gym_document(trajectory_id: str, task: str, *, schema_version: str) -> dict[str, Any]:
    """One ATIF document shaped like a `gym eval export atif` file: no metrics, no media."""
    return atif.trajectory(
        schema_version=schema_version,
        trajectory_id=trajectory_id,
        task=task,
        score_hint=task,
    )


def export_files(
    producer: Producer,
    *,
    entries: int = 2,
    rich_fields: bool | None = None,
    unlisted: bool = True,
) -> dict[str, bytes]:
    """One producer's export: `entries` documents, and the manifest if it ships one.

    `rich_fields` overrides the producer's own field profile, so field handling can be
    compared on one producer's naming and joining: the same text with and without
    token metrics and media must score identically.
    """
    rich = producer.rich_fields if rich_fields is None else rich_fields
    files: dict[str, bytes] = {}
    rows: list[dict[str, Any]] = []
    for index in range(entries):
        trajectory_id = f"t{index}"
        task = f"Summarise record {index}."
        document = (
            _harbor_document(trajectory_id, task, rich_fields=rich)
            if producer.schema_version == atif.V16
            else _nemo_gym_document(trajectory_id, task, schema_version=producer.schema_version)
        )
        payload = atif.encoded(document)
        name = producer.file_pattern.format(index=index)
        files[name] = payload
        if producer.manifest:
            row = atif.manifest_row(name, 0, index, payload if producer.digests else None)
            rows.append(row)
    if producer.manifest:
        body = "".join(f"{json.dumps(row, sort_keys=True)}\n" for row in rows)
        files[atif.MANIFEST_FILE] = body.encode()
    if unlisted and not producer.manifest:
        # A producer that ships no manifest is discovered recursively, so a file that is
        # not a trajectory at all has to be ignored rather than counted or scored.
        files["notes.json"] = b"not an ATIF trajectory\n"
    return files
