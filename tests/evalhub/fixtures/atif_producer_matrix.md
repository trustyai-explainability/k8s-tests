# ATIF producer × difference class matrix

One row per ATIF producer: which check covers each of the three ways a producer's
export differs from another's, and what this release does about it. Every cell that
names a check is a node id in this repository, and
`test_the_matrix_only_names_checks_that_run` fails when one does not resolve — so a
row cannot claim coverage it does not have. A cell that says `unvalidated` is exempt
on purpose: the producer has no real export to test against yet, and recording that
is worth more than a fixture invented to match a guess.

The matrix is the deliverable of RHAI-22313 under RHAISTRAT-2813 (HLR-5).

| Producer | Joining and naming | Provenance | Field handling | Disposition |
| --- | --- | --- | --- | --- |
| Harbor | `tests/evalhub/test_evalhub_atif.py::test_a_directory_export_is_discovered_without_a_manifest` | `tests/evalhub/test_evalhub_atif.py::test_a_directory_export_carries_the_hash_of_the_bytes_read` | `tests/evalhub/test_evalhub_atif.py::test_a_producers_field_profile_does_not_move_the_score` | validated: ATIF-v1.6, `*.trajectory.json`, discovered by content |
| NeMo Gym | `tests/evalhub/test_evalhub_atif.py::test_the_manifest_drives_discovery` | `tests/evalhub/test_evalhub_atif.py::test_the_run_records_manifest_provenance` | `tests/evalhub/test_evalhub_atif.py::test_a_producers_field_profile_does_not_move_the_score` | validated: manifest is authoritative, no adapter change needed for class 3 |
| OpenHands | unvalidated: no export is available in any repository | unvalidated: no export is available in any repository | unvalidated: no export is available in any repository | unvalidated: obtain a real export before claiming the row |
| Claude Code | unvalidated: no export is available in any repository | unvalidated: no export is available in any repository | unvalidated: no export is available in any repository | unvalidated: obtain a real export before claiming the row |
| Training Hub | unvalidated: ATIF-v1.7 writer still in progress (RHAISTRAT-2815) | unvalidated: ATIF-v1.7 writer still in progress (RHAISTRAT-2815) | unvalidated: ATIF-v1.7 writer still in progress (RHAISTRAT-2815) | unvalidated: record the row, validate when the writer ships |

## Golden values

`atif_golden.json` next to this file records the values the adapter emits for the
Harbor export under the section's deterministic stub judge, so a change to the
adapter's scoring or accounting fails a check instead of moving a number quietly.
The ticket that asked for it named `community-atif:v0.7.0` as the shipped tag; the
operator pins `v1.1.0-alpha.1` since the 1.1.x SDK release (eval-hub-sdk#203,
`trustyai-service-operator#1138`), and the golden file records the tag it was
captured from rather than the ticket's.
