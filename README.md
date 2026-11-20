# k8s-tests

[![Python lint](https://github.com/trustyai-explainability/k8s-tests/actions/workflows/python-lint.yaml/badge.svg?branch=main)](https://github.com/trustyai-explainability/k8s-tests/actions/workflows/python-lint.yaml)

Kubernetes-level tests and harnesses for the TrustyAI components.

## Local Kind harness

`Makefile` brings up a local [Kind](https://kind.sigs.k8s.io/) cluster and runs
the TrustyAI module and workload operators on it: it builds both operator images
from a `trustyai-service-operator` checkout, loads them into the cluster, and
deploys the module operator with its image values overridden to the images it
just built. The module operator then renders and deploys the workload operator
from the manifests baked into its image.

The harness stops at the operators: `make up` also creates the `TrustyAI` module
resource, which makes the module operator deploy the workload operator, and waits
until both are running the images it built. The resource is built in Python rather
than from a manifest, so no YAML in this repository has to be kept in step with the
operator. What the checks then look at - `TrustyAIService`, `EvalHub` instances, the
jobs that call a model - is created by the suite, the way a user would, so the
operand lifecycle is under test.

### Prerequisites

- `kind` and `kubectl` 1.24 or newer, which the checks need to mint bearer
  tokens with `kubectl create token`
- `podman` or `docker` to build the images
- `uv`, which resolves the pinned Python 3.11+ dependencies from `uv.lock` for
  `make deploy`, `make test`, `make test-evalhub-adapters`, `make lint` and
  `make fmt`
- `helm` 3.x, only for `make openshell` and `make up OPENSHELL=1`
- `python3` with the [`cryptography`][cryptography] package, which `make deploy`
  runs `hack/gen-certs.py` with to create the TLS secrets Kind does not issue
- a `trustyai-service-operator` checkout, passed as `OPERATOR_REPO`

[cryptography]: https://cryptography.io/

### Quick start

```sh
make up OPERATOR_REPO=/path/to/trustyai-service-operator
make status     # both operators, and no operands
make test       # creates the operands and checks them
```

`make up` is the four steps below, run in order; each is also usable on its own:

```sh
make cluster    # create the Kind cluster (idempotent)
make images     # build the workload and module operator images from OPERATOR_REPO
make load       # load both images into the cluster
make deploy     # deploy the module operator, pointing it at the built images
```

`up`, `images` and `deploy` require `OPERATOR_REPO` and stop with a pointer to
the flag when it is missing or does not look like an operator checkout.

Add `OPENSHELL=1` to `make up` to install the [OpenShell
gateway](#openshell-gateway-optional) after the operators, or run `make openshell`
directly (it creates the cluster if needed). It is off by default so the existing
workflow does not need Helm or the gateway's cluster-scoped prerequisite.

Tear the cluster down with `make down`; `make clean` also deletes the images
the harness built and the HTML reports (`reports/`), without touching the
cluster.

### What `make deploy` does

1. Creates the target namespace (`system` by default).
2. Applies the external CRD stand-ins the operators expect, copied from
   `$OPERATOR_REPO/tests/crds`: OpenShift `Route`, Prometheus Operator
   `Prometheus`/`ServiceMonitor`, and KServe `InferenceService`.
3. Renders `$OPERATOR_REPO/trustyai-operator-module/config/default` from a
   temporary copy, so the module operator image, the workload operator image it
   renders (`RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_OPERATOR_IMAGE`), the EvalHub
   image the workload operator resolves (`RELATED_IMAGE_ODH_EVAL_HUB_IMAGE`,
   when `EVALHUB_IMG` is set) and the namespace are all overridden without
   touching the checkout.
4. Waits for the module operator to come up, seeds the `Prometheus` instance it
   expects, and creates the webhook and metrics TLS secrets that OpenShift's
   service CA would normally issue (Kind has no service CA).
5. Applies the `TrustyAI` module resource from `utilities/stack.py`, which builds
   it in Python - there is no manifest - and waits for the workload operator the
   module operator deploys from it: the Deployment, the image it runs and the
   `RELATED_IMAGE_*` values it resolves, and its rollout.
6. Points the `TrustyAIService` and `EvalHub` CRD conversion webhooks at the seeded
   CA, which OpenShift's service CA would fill in.

That is where the harness stops: both operators run, with the images this run asked
for, and nothing is reconciled on top of them. `make status` right after `make up`
shows the two operators and no operands.

### OpenShell gateway (optional)

`make openshell` puts the [OpenShell][openshell] gateway on the Kind cluster,
creating the cluster if it is not up; `make up OPERATOR_REPO=... OPENSHELL=1`
installs it after the operators. It is opt-in because the default Kind CNI cannot
enforce the isolation the gateway is for, not because it cannot run: see [What
Kind cannot prove](#what-kind-cannot-prove) before recording an isolation result
from it.

It installs two things, both idempotently:

1. the [Agent Sandbox][agent-sandbox] controller and its `Sandboxes` CRD from
   `AGENT_SANDBOX_MANIFEST`, because the gateway's Kubernetes driver creates a
   `Sandbox` and lets that controller create the workload Pod;
2. the OpenShell Helm chart from GHCR, `OPENSHELL_CHART` at
   `OPENSHELL_CHART_VERSION` (0.1.4, the version the sandbox work targets), into
   `OPENSHELL_NAMESPACE`, with `workspaceMode: shared`, the release namespace as
   the sandbox namespace, and the default StatefulSet workload. The gateway keeps
   its SQLite database on a PVC and each sandbox gets a workspace PVC, both on the
   cluster's default StorageClass.

```sh
make openshell
make openshell-status
kubectl --context kind-trustyai -n openshell port-forward svc/openshell 8080:8080
make openshell-uninstall   # Helm release only; the namespace and PVCs remain
```

A local Kind cluster has no OIDC provider or user directory, so the gateway is
installed with `server.auth.allowUnauthenticatedUsers=true`
(`OPENSHELL_ALLOW_UNAUTHENTICATED=true`). Set it to `false` when the test is about
the gateway's authentication rather than what runs behind it. `make down`
removes the gateway, the Agent Sandbox controller and its CRDs with the cluster;
`make clean` does not, because it only removes the images and reports.

#### What Kind cannot prove

- **NetworkPolicy is not enforced.** Kind's default CNI, kindnet, accepts
  `NetworkPolicy` objects but does not enforce them. OpenShell's prerequisites
  require a CNI that enforces ingress and egress policy in every sandbox
  namespace; without one, sandbox workloads can reach the network directly and
  bypass the supervisor. The gateway and its API run, but any deny/allow or
  cross-tenant isolation result is meaningless. Upstream's own Kind e2e replaces
  kindnet with Calico before it asserts policy; this harness does not, and warns
  when it finds no Calico or Cilium pods.
- **No SCC, PSA, SELinux, OVN or audit.** Kind does not model OpenShift's
  namespace UID allocation, MCS/SELinux labels, OVN network policy or audit log,
  and the driver falls back to its default sandbox UID instead of the namespace's
  `openshift.io/sa.scc.uid-range`. A denial Kind cannot produce may exist on
  OpenShift, and a permissive result here says nothing about the platform.
  Re-run security-relevant checks on OpenShift.
- **The sandbox runtime has its own kernel requirements.** OpenShell fails sandbox
  startup when the node cannot install a nested seccomp user-notification filter
  or use Landlock; that is why it requires OpenShift 4.19+, whose RHCOS kernel has
  Landlock (4.16-4.18 do not). A Kind node runs the container host's kernel (the
  Podman or Docker VM on macOS), so the gateway becoming Ready does not prove a
  sandbox will start. Exercise one sandbox before relying on sandbox results.
- **One node and node-local storage.** Kind is a single control-plane node with a
  `local-path` StorageClass. Sandbox PVCs are node-local and scheduling, HA,
  eviction and multi-node storage behaviour are not exercised.
- **Runtime pulls.** The gateway, sandbox and supervisor images come from
  `ghcr.io/nvidia/openshell`, and the default workload sandbox image is
  `nvcr.io/nvidia/base/ubuntu:24.04`. They are published for `amd64` and `arm64`,
  but are pulled when a sandbox starts rather than preloaded, so the first
  sandbox can spend minutes pulling. A registry that needs credentials needs
  `sandboxImagePullSecrets`, which this target does not set.
- **A moving prerequisite.** `AGENT_SANDBOX_MANIFEST` defaults to the latest
  Agent Sandbox release, as OpenShell's install guide does. Pin it, together with
  `OPENSHELL_CHART_VERSION`, when a result has to be reproducible. The chart's
  preflight fails if the Agent Sandbox `Sandboxes` API is not served, and the
  install needs cluster-admin, which the Kind kubeconfig is.

[openshell]: https://docs.nvidia.com/openshell/latest/kubernetes/
[agent-sandbox]: https://agent-sandbox.sigs.k8s.io/

### The module resource and the operands

The module resource is built by `utilities/trustyai.py:module_manifest`:
`default-trustyai`, `managementState: Managed`, with TAS, LMES, EvalHub and NeMo
Guardrails enabled. `make deploy` applies it through `utilities/stack.py`; the
`trustyai_module` fixture in `tests/conftest.py` then adopts it rather than
rewriting it, and only creates it when the cluster has none - a cluster brought up
without `make deploy`, which is also the only case where the session deletes it
again (unless `KEEP_RESOURCES` is set). A resource that is already there, on a
platform-managed install for instance, is never touched.

Those four are the harness's *capability*, not a list of what is covered: the
module resource is a cluster-scoped singleton whose `enabledServices` becomes the
workload operator's `--enable-services` argument, with every controller in that one
manager process, so a service can only be switched on here. All four are on so a
check can create whatever operand it needs, in a namespace of its own - the way
`opendatahub-tests` does - rather than because a check exists for each today.

Everything on top of the operators is created by the suite, the way a user or the
platform operator would, so there is no manifest for any of it:

- an `EvalHub` instance with its namespace and TLS material, and a
  `TrustyAIService`, each in its own namespace from a fixture in `tests/conftest.py`;
- the LLM emulator, or the model credential Secret when `MODEL_URL`/`MODEL_KEY`
  name a real endpoint, and the resources a job needs in its tenant namespace.

The fixtures depend on `trustyai_module`, so no operand is created before the
workload operator that reconciles it is up; the fixture repeats the waits
`utilities/stack.py` does, which is what makes a bare `make test` - without a fresh
`make deploy` - wait for a stack someone else brought up instead of racing it.

### Bringing a workload of your own

The operators are the session's, a workload is the check's. Three fixtures make that
the default shape of a new section, and each removes what it created when the check
that asked for it finishes:

| Fixture | Gives you |
| --- | --- |
| `workload_namespaces(request, prefix=...)` | a namespace for a resource you bring yourself |
| `trustyai_services(request)` | a `TrustyAIService` in a namespace of its own, waited for, with port-forwards to its API (pass `namespace=` to reuse one) |
| `evalhub_tenants(request)` | a tenant-labelled namespace for an EvalHub job |

So a section that needs its own copy of something asks for one, in its own
namespace, instead of sharing the session's:

```python
@pytest.fixture(scope="module")
def my_service(
    request: pytest.FixtureRequest,
    trustyai_services: Callable[..., ServiceAddress],
) -> ServiceAddress:
    """One service for this module; a check that needs a second one asks again."""
    return trustyai_services(request)


def test_an_lmeval_job(request: pytest.FixtureRequest, workload_namespaces, custom_api) -> None:
    namespace = workload_namespaces(request, prefix="trustyai-lmeval")
    custom_api.create_namespaced_custom_object(
        group="trustyai.opendatahub.io",
        version="v1alpha1",
        namespace=namespace,
        plural="lmevaljobs",
        body={
            "apiVersion": "trustyai.opendatahub.io/v1alpha1",
            "kind": "LMEvalJob",
            "metadata": {"name": "arc-easy", "namespace": namespace},
            "spec": {"model": "hf", "taskList": {"taskNames": ["arc_easy"]}, "limit": "1", "allowOnline": True},
        },
    )
```

A second `TrustyAIService` does not collide with the first: the forwards ask
`utilities/portforward.py` for a free port pair, starting at
`TRUSTYAI_SERVICE_API_PORT`/`TRUSTYAI_SERVICE_HEALTH_PORT` (18443/18080), and give
them back at teardown. What a check cannot have of its own is a service that is not
enabled on the module resource: `enabledServices` is the one cluster-wide switch, so
`LMEvalJob`, `EvalHub` and guardrails workloads work because all four are on. The
`LMEvalJob` shape above is the operator's own sample
(`config/samples/lmes_v1alpha1_lmevaljob_dra.yaml`), minus the DRA resource claim;
a marker of your own belongs in `pyproject.toml` next to the others.

### Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPERATOR_REPO` | *(required)* | Checkout the images and manifests come from |
| `KIND_CLUSTER` | `trustyai` | Kind cluster name (context `kind-<name>`) |
| `KIND_NODE_IMAGE` | Kind's default | Pin a node image, e.g. `kindest/node:v1.29.0` |
| `KIND_PROVIDER` | auto | Force `podman` or `docker` on hosts that have both |
| `HELM` | `helm` | Helm binary the OpenShell target runs |
| `OPENSHELL` | *(empty)* | `1` makes `make up` install the gateway as well |
| `OPENSHELL_NAMESPACE` | `openshell` | Namespace the gateway and its sandboxes run in |
| `OPENSHELL_RELEASE` | `openshell` | Helm release name |
| `OPENSHELL_CHART` | `oci://ghcr.io/nvidia/openshell/helm-chart` | Chart reference |
| `OPENSHELL_CHART_VERSION` | `0.1.4` | Chart version; its `appVersion` picks the matching gateway images |
| `OPENSHELL_ALLOW_UNAUTHENTICATED` | `true` | Admit clients without OIDC, since Kind has no identity provider; set `false` for auth tests |
| `AGENT_SANDBOX_MANIFEST` | latest Agent Sandbox release | Controller and CRD manifest; pin it together with the chart version for reproducible runs |
| `NAMESPACE` | `system` | Namespace both operators are deployed into |
| `KUBECTL` | `kubectl` | kubectl the harness and the checks shell out to, including the token the API checks mint |
| `MODEL_URL` | *(empty)* | OpenAI-compatible base URL the job checks call instead of the emulator; setting it stops the fixtures deploying the emulator |
| `MODEL_KEY` | *(empty)* | Credential for `MODEL_URL`; the fixtures write it to the `model-credentials` Secret under `api-key`, which the job names in `model.auth.secret_ref` |
| `MODEL_NAME` | `TinyLlama/TinyLlama-1.1B-Chat-v1.0` | Model name a job submits |
| `WORKLOAD_IMG` | `localhost/trustyai-service-operator:dev` | Workload operator image reference |
| `MODULE_IMG` | `localhost/trustyai-operator-module:dev` | Module operator image reference |
| `EVALHUB_IMG` | *(empty)* | EvalHub operand image; empty keeps the operator's own pin (`quay.io/evalhub/evalhub:latest`) |
| `TRUSTYAI_SERVICE_IMG` | `quay.io/trustyai/trustyai-service:latest` | TrustyAI service operand image, passed through `RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_PY_IMAGE`; set it to a build under test |
| `TAG` | `dev` | Tag used by both image references |
| `IMAGE_PREFIX` | `localhost` | Registry/repository prefix used by both image references |
| `ENGINE` | auto | `podman` or `docker`; `docker` wins when both are installed, so set `ENGINE=podman` to build, save and remove the images with podman |
| `TIMEOUT` | `300s` | Timeout for each readiness wait |
| `PYTEST_ARGS` | *(empty)* | Extra pytest arguments, e.g. `-m module`, `-m lmeval`, `-m adapters` or `-k evalhub` |
| `REPORT_FILE` | `reports/report.html` | Report written by `make test` and `make test-all` |
| `ADAPTERS_REPORT_FILE` | `reports/adapters.html` | Report written by `make test-evalhub-adapters` |
| `KEEP_RESOURCES` | *(empty)* | Leave everything the run created on the cluster: see [Keeping the resources](#keeping-the-resources) |

The LLM emulator's `EMULATOR_IMG`, `EMULATOR_NAMESPACE` and `EMULATOR_TOKEN` are
documented under [LLM emulator](#llm-emulator).

### Notes and troubleshooting

- `make up` leaves both operators running and no operands, so `make status` shows
  the module operator, the workload operator and no `EvalHub` instance. If the
  workload operator never appears, the module operator could not reconcile the
  module resource: `make logs` tails the module operator, and
  `kubectl get trustyais.components.platform.opendatahub.io default-trustyai -o yaml`
  shows the conditions it reported.
- `make deploy` converges the module resource on the services the harness needs, so
  re-running it resets one that was edited by hand. A test session never does that:
  it adopts the resource it finds.
- Image tags other than `latest` are important: `latest` would make Kubernetes
  pull instead of using the image loaded into the node.
- The operand images the operator pins are published for `linux/amd64` only:
  `quay.io/opendatahub/odh-kube-rbac-proxy:odh-stable`, and `ta-lmes-job` and
  `ta-lmes-driver` in the same overlay. On an arm64 host, such as an Apple Silicon
  laptop, the operand pods stay in `ImagePullBackOff` (`no match for platform in
  manifest`) and the rollout wait fails with the pod's reason in the timeout
  message. Point the run at an amd64 cluster, or give the module operator an arm64
  build of the proxy through `RELATED_IMAGE_ODH_KUBE_RBAC_PROXY_IMAGE`
  (`kubectl -n system set env deployment/trustyai-operator-module-controller-manager
  RELATED_IMAGE_ODH_KUBE_RBAC_PROXY_IMAGE=<ref>`, then touch the module resource so
  the workload operator is rendered again). The upstream
  `quay.io/brancz/kube-rbac-proxy` is multi-arch but lacks the ODH
  `--tls-curve-preferences` flag, so it cannot stand in for it.
- On hosts without the `docker` CLI the harness exports each image with
  `podman save` and loads it with `kind load image-archive`, since
  `kind load docker-image` always shells out to `docker`.
- `make images` passes `--load` to `docker build`. The `docker` CLI builds with
  buildx, and when the active builder uses the `docker-container` driver a build
  without `--load` prints `No output specified with docker-container driver` and
  leaves the image only in the builder's cache, so `make load` fails with
  `Error response from daemon: localhost/trustyai-service-operator:dev: image not
  known`. `ENGINE=podman` sidesteps buildx and its builder configuration
  altogether.
- The module image bakes the workload operator manifests at build time. If
  `config/` and `trustyai-operator-module/config/manifests-template/` have
  drifted, `make images` warns; refresh the template with
  `make -C $OPERATOR_REPO sync-trustyai-module-manifests`.
- `make logs` tails the module operator; the workload operator has its own
  pods in the same namespace (`-l control-plane=trustyai-service-operator`),
  and EvalHub runs as `-l app=eval-hub`.
- `make openshell` waits for the gateway pod; a pod stuck in `Pending` is
  usually storage, not OpenShell. Both the gateway's SQLite volume and each
  sandbox's workspace volume claim the default StorageClass, so check
  `kubectl get storageclass` and set `server.dataStorageClass` /
  `server.workspaceStorageClass` if none is marked default. A Helm failure
  naming `agentSandbox.preflight` means the `Sandboxes` CRD is not established;
  `make openshell-status` shows the Agent Sandbox controller.
- `make openshell-uninstall` removes the Helm release only. The `openshell`
  namespace, its retained PVCs and the cluster-scoped Agent Sandbox controller
  and CRDs remain until `make down` deletes the cluster.
- Every instance is named `evalhub` and reconciles every tenant-labelled
  namespace, including namespaces it did not create, so a `KEEP_RESOURCES` run or
  an interrupted one leaves an instance that keeps writing to later runs'
  namespaces. The run grants its own job access under a name the operator does not
  manage, so it does not depend on who reconciled last, and it logs a warning when
  it sees more than one instance. Delete what you do not need
  (`kubectl get evalhubs -A`).
- The LLM emulator (`vllm-emulator`) is deployed by the fixtures into the
  namespace under test and removed when the run ends; a job reaches it in-cluster
  as `vllm-emulator:8000`, which is what the benchmark and adapter checks
  submit. While a run is in flight it can be poked at from the host with
  `kubectl -n system port-forward svc/vllm-emulator 8000`.
- The EvalHub tests create their own instance with the sqlite database backend,
  since Kind has no PostgreSQL, and mint its `evalhub-tls` certificate and
  `evalhub-service-ca` bundle, which OpenShift's service CA would provide. The
  EvalHub image is pulled from the registry rather than loaded into the node, so
  the first `make test` needs network access; the adapter checks also fetch their
  benchmark's dataset from the Hub. `make up EVALHUB_IMG=<ref>` points
  the instances those tests create at another image, for example one built from
  `eval-hub`.

## LLM emulator

The checks that call a model use an emulator by default: the `vllm_emulator`
image, which answers OpenAI-shaped `/v1/completions` and `/v1/chat/completions`
without a GPU.

Set `MODEL_URL` to call a real, OpenAI-compatible endpoint instead. The fixtures
then deploy no emulator, so the checks run against a cluster that already serves
a model, and the emulator checks skip. `MODEL_KEY`, when set, is the credential
that endpoint requires: the fixtures create a `model-credentials` Secret in the
tenant namespace with the value under `api-key`, the key a job's
`model.auth.secret_ref` resolves, so the job payload never carries it.
`MODEL_NAME` overrides the model name a job submits.

```sh
make test MODEL_URL=http://model.example.svc:8000/v1 MODEL_KEY=sk-...
make test MODEL_URL=http://model.example.svc:8000/v1 MODEL_NAME=granite-3b
```

Without `MODEL_URL`, `MODEL_KEY` is also the token the emulator requires, so the
credential plumbing can be exercised with a key of your own.

The tests own the emulator. `tests/conftest.py` deploys it into the namespace
under test (and removes it afterwards), so `make up` leaves no emulator behind
and a run cannot depend on one someone left in the cluster. The image, namespace,
port and token are parameters:

```sh
make test                                  # default emulator, namespace and token
make test EMULATOR_NAMESPACE=my-ns         # somewhere else
make test EMULATOR_IMG=quay.io/.../vllm_emulator:<tag>
make test EMULATOR_TOKEN=another-token
```

The emulator image has no notion of credentials, so the fixture mounts a wrapper
over it from a ConfigMap (`hack/emulator/auth_emulator.py`) and starts that
instead. Every `/v1` request must then carry `Authorization: Bearer <token>`,
where the token is the `api-key` entry of the `vllm-emulator-auth` Secret - the
key an EvalHub job's `model.auth.secret_ref` resolves. `/health` and
`/__auth/state` stay open: the first is the probe, the second reports how many
requests were authenticated or rejected and the fingerprints of the tokens that
arrived (never the tokens), so a check can tell a forwarded credential from a
dropped one.

## Benchmark job

`tests/evalhub/test_evalhub_benchmark.py` submits one real evaluation job through the
EvalHub API and reads its results:

- a ServiceAccount in a tenant namespace, granted the API by the RBAC the proxy
  authorizes against, minted as a bearer token by `kubectl create token`;
- a `model.auth.secret_ref` Secret in that namespace carrying `api-key`: the
  emulator's token, or `MODEL_KEY` when set. With `MODEL_KEY` the Secret has the
  known name `model-credentials`; otherwise it is `emulator-credentials`;
- the instance certificate copied into the tenant's `evalhub-service-ca`
  ConfigMap, which OpenShift's service CA would fill in and Kind cannot, so the
  evaluation sidecar can verify the instance it reports back to;
- the `arc_easy` benchmark of the `lm_evaluation_harness` provider with
  `num_examples: 1`, against the emulator's in-cluster URL or `MODEL_URL` when
  that is set.

It is the harness's own end-to-end check of the API, the sidecar and the
credential, so it stays in `make test`. Benchmarks that are the point of the run
rather than a check of the plumbing live in [EvalHub adapters](#evalhub-adapters),
one per entry in its table.

The job runs in the cluster: the adapter container calls the model through the
EvalHub sidecar, which injects the credential. The checks assert the job reaches
`completed`, that `results.benchmarks[0]` carries the benchmark id, provider and
numeric metrics, and, when the model is the emulator, that it saw the tenant
Secret's token on a `/v1` path. A real endpoint does not report what it
authenticated, so that last check skips rather than pretending the job's own
verdict proves the credential arrived. The first run of the day is the slow one -
the adapter image has to be pulled and the job scheduled (about six minutes on a
cold Kind node); afterwards the run is about a minute. It is marked `benchmark`,
so it can be filtered out:

```sh
make test PYTEST_ARGS="-m 'not benchmark'"
```

## EvalHub adapters

`tests/evalhub/test_evalhub_adapters.py` is the section that runs real benchmarks. Every
entry in its `BENCHMARKS` table is one adapter run: submitted through the API,
waited for until the adapter finishes in the cluster, and read back as a result.
It covers one today, `ethics_cm` (commonsense moral judgment) of the
`lm_evaluation_harness` provider with `num_examples: 1`; adding a benchmark is one
more entry in that table, and every check then runs once for it, under a
`[<benchmark>]` id. Its jobs call the same endpoint the benchmark job does: the
emulator the fixtures deploy, or `MODEL_URL` when that is set, with `MODEL_KEY` as
the credential.

For each benchmark the section asserts that the job is accepted with exactly that
benchmark, its provider and its `num_examples`; that the job and the benchmark
alone reach `completed`; that `results.benchmarks[]` carries the benchmark, its
provider and numeric metrics; that the metric the provider config scores it on
(`acc` for `ethics_cm`) is among those metrics and is a rate; that the provider's
pass criteria turn the score into a `test` block; and, when the model is the
emulator, that it authenticated the job's model requests, so the model was called
rather than the job merely reporting success.

The section carries `evalhub` and `adapters`: it is part of the EvalHub selection,
and `adapters` names the section within it. Because an adapter run costs minutes
and fetches its benchmark's dataset from the Hub, it is the one section the default
run excludes - `make test` is `-m 'not adapters'` and `make test-evalhub` is
`-m 'evalhub and not adapters'` - so it is not selected by accident, only by name:

```sh
make test-evalhub-adapters                    # only this section, into reports/adapters.html
make test-evalhub-adapters KEEP_RESOURCES=1   # leave the job, its pod and the adapter logs behind
make test-all                                 # every section, the adapters included
make test-select SELECT='-m adapters'         # the same selection, into the usual report
```

## Collection generation authorization

`tests/evalhub/test_evalhub_collection_generation.py` covers the authorization
`trustyai-service-operator#1131` added for the collection generation API: `POST
/api/v1/evaluations/collections/generation` and `GET`/`DELETE
/api/v1/evaluations/collections/generation/{id}`. kube-rbac-proxy authorizes every
EvalHub request with a SubjectAccessReview in the namespace the `X-Tenant` header
names, against a pseudo-resource and verb the operator writes into the `auth.yaml`
key of the instance's configuration ConfigMap; the checks read that policy from the
ConfigMap the proxy container actually mounts, and the tenant Role from the tenant
namespace, so they assert what the cluster runs rather than a copy of it.

Four independent things can regress, and each is checked on its own:

- the policy maps both generation entries to `evaluations` (`create` for the
  POST, `{{.FromMethod}}` for the lifecycle entry) with the `X-Tenant` rewrite,
  declares each path once, and maps nothing for the removed
  `.../generation/models` endpoint;
- the generation entries are declared **before** the generic
  `/api/v1/evaluations/collections/*` entry, which also maps `get` and `delete`:
  kube-rbac-proxy uses the first endpoint that matches, so the other order would
  authorize a generation request as a collection. The check reads the wildcard's
  methods and states the overlap rather than assuming it, so it reports when the
  ordering stops mattering;
- the pod template's `auth.yaml` digest annotation describes the policy the
  container mounts, so a pod is never running one policy while a later reconcile
  has already replaced the ConfigMap under it;
- the tenant-user Role the operator writes keeps the lifecycle inside
  `evaluations`: it grants `get`, `create` and `delete` there and no collection
  write, so generating a collection does not hand the tenant the right to write
  one.

The API checks then call the three paths through the port-forward as an identity
granted exactly those tenant-user rules, whose only `collections` verbs are `get`,
`list` and `watch`. That is what makes them discriminating: a `DELETE` or a `POST`
that fell through to the generic collections entry would ask for a `collections`
verb the identity does not have and the proxy would answer `403`. They also assert
the paths need a token, unlike the health path, and that the same token is refused
when `X-Tenant` names a namespace it has no grant in. The section carries the
`generation` marker as well as `evalhub`, so it stays in the EvalHub block of the
report and can be selected on its own:

```sh
make test-evalhub-generation                  # this section only
make test-select SELECT='-m generation'       # the same, spelled as a selection
make test-select SELECT='-k tenant_role'      # or one check of it, by name
```

## LMEvalJob

`tests/lmeval/test_lmeval_network_policy.py` creates an `LMEvalJob` itself and checks what
the workload operator builds around it: a Pod running lm-evaluation-harness, and a
NetworkPolicy whose selector is that Pod's identity labels
(`trustyai.opendatahub.io/component: lmes`, `role: evaluation`, `owner-uid: <job
uid>`, plus the `lmevaljob-uid` label the first, ingress-only policy selected on).
The policy admits no ingress and one allow-all egress rule, which is the operator's
documented intent: an evaluation reaches arbitrary model endpoints, tokenizer and
dataset downloads, S3 and OCI, so an allowlist is not something it can derive.

The section carries the `lmeval` marker, and `tests/lmeval/` holds it the way
`tests/evalhub/` holds the EvalHub sections: a directory per component, a marker per
section within it. The `lmes` names above are the operator's own - its `lmes` service
and the `component: lmes` label value - not the marker.

The contract under test is convergence, not the evaluation. On every reconcile the
operator re-derives the policy and the Pod's identity, so deleting the policy,
clearing its rules, or taking an identity label off the Pod has to be repaired - a
running evaluation that loses its selector is one nothing reports. A *completed* job
is the hard case and the one that regresses first: there is no polling interval left
to fall back on, so the repair has to come from watching the Pod.

The Pod is deliberately held out of the scheduler by a CPU request no Kind node can
satisfy, so nothing is pulled or run: the convergence happens before the first
container starts, and a real benchmark would need a model and a dataset to prove the
same thing. The completed-job case is reproduced the way the operator's own suite
does it, by writing the status a finished run reports, because that is the input the
repair path reacts to.

```sh
make test-lmeval    # only this section
```

## TrustyAI service

`tests/test_trustyai_service.py` creates a `TrustyAIService` itself, waits for
the operand the workload operator builds from it, and then talks to the API the
way a user would: through the TLS Service, with a bearer token.

The service image matters, because the operator's ODH manifest pins
`quay.io/opendatahub/trustyai-service:latest`, the retired Java build. That image
mounts a serving key the current deployment template no longer provides and
crash-loops on `/etc/tls/internal/tls.crt`. The harness therefore passes
`TRUSTYAI_SERVICE_IMG` (the Python service by default) through
`RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_PY_IMAGE`, and `make test` asserts the
operand runs exactly that image:

```sh
make up OPERATOR_REPO=... TRUSTYAI_SERVICE_IMG=quay.io/you/trustyai-service@sha256:...
```

Three pieces are seeded by the fixtures because the platform supplies them on
OpenShift and Kind does not: the two serving certificates (`<name>-tls` and
`<name>-internal`, which the service CA issues from the serving-cert
annotations), and a ServiceAccount with `get` on the Service, which the proxy's
SubjectAccessReview checks. The API is reached over a port-forward, so the
certificate covers `localhost` and the checks verify the chain rather than
disabling it.

Two conditions the resource reports are platform-only here and asserted as such
rather than faked: `RouteAvailable` (`RouteNotFound`, since nothing admits the
Route) and `InferenceServicesPresent` (no model server registers), which is why
`Ready` stays `False` while the claim and the operand are available.

## Pytest suite

`tests/` holds the pytest checks that run against a live cluster; they are kept
separate from the harness so they can point at any cluster that runs the same
operators.

| Module | What it checks |
| --- | --- |
| `tests/test_trustyai_operators.py` | the harness smoke checks: both operator Deployments exist, ask for a single replica, and are healthy (`Available`, the desired replicas ready, their pods `Running` with every container ready) |
| `tests/test_trustyai_module.py` | the modular stack: the module CRD is installed, both operator Deployments are available, `TrustyAI/default-trustyai` reports `Ready=True` with `ProvisioningSucceeded=True` and an up-to-date `observedGeneration`, and the module operator runs the built image |
| `tests/test_trustyai_workload.py` | the workload operator image: it matches the `RELATED_IMAGE_*` value the module operator renders it from, the image the harness built, and the operator's own ConfigMap |
| `tests/test_emulator.py` | the LLM emulator (skipped when `MODEL_URL` is set): its Deployment runs the pinned image and is healthy, `/health` needs no token, `/v1/completions` and `/v1/chat/completions` answer `401` without the token or with another one and `200` with the token from the `vllm-emulator-auth` Secret, that Secret carries the `api-key` key `model.auth.secret_ref` resolves, and the token check reports which token fingerprint it saw |
| `tests/evalhub/test_evalhub_benchmark.py` | a benchmark job: the API accepts a job for `arc_easy` with `num_examples: 1` against the emulator or `MODEL_URL`, it reaches `completed`, its results carry the benchmark id, provider and numeric metrics, and - against the emulator - the emulator saw the tenant Secret's token (or `MODEL_KEY`) on a `/v1` request |
| `tests/evalhub/test_evalhub_adapters.py` | the EvalHub adapters section (opt-in, `make test-evalhub-adapters`): each benchmark in its table - currently `ethics_cm` of `lm_evaluation_harness`, with `num_examples: 1` against the emulator or `MODEL_URL` - is accepted as the job's only benchmark, reaches `completed`, reports the benchmark, provider, numeric metrics and the `acc` its provider scores it on, turns its pass criteria into a test outcome, and - against the emulator - the emulator authenticated the model requests it made |
| `tests/evalhub/test_evalhub_collection_generation.py` | the authorization `trustyai-service-operator#1131` added for the collection generation API: the mounted policy maps `POST /api/v1/evaluations/collections/generation` to `evaluations create` and `GET`/`DELETE .../generation/{id}` to `evaluations` `{{.FromMethod}}`, each declared once with the `X-Tenant` rewrite and ahead of the generic `/api/v1/evaluations/collections/*` entry that also maps `get` and `delete`; the removed `.../generation/models` path has no mapping; the pod template's `auth.yaml` digest describes the policy the container mounts; the tenant-user Role grants `evaluations` `get`/`create`/`delete` and no collection write; and, as an identity granted exactly those rules, all three paths answer through the proxy (not `401`/`403`), need a token, and are refused when `X-Tenant` names a namespace the identity has no grant in |
| `tests/lmeval/test_lmeval_network_policy.py` | an `LMEvalJob`: the operator creates one NetworkPolicy, owned by the job, admitting no ingress and one allow-all egress rule, selecting the Pod on the identity labels it also stamps there; taking any of the four identity labels off the Pod is repaired (also for a completed job, where only the Pod watch is left to trigger it); editing the policy's rules is repaired while an annotation the operator does not own is kept; and deleting the policy brings a replacement with a new UID rather than reviving the old one |
| `tests/test_trustyai_service.py` | the TrustyAI service: the CRD serves `v1` and `v1alpha1` through a conversion webhook and a resource created as one reads back as the other; the operand rolls out one replica with the `trustyai-service`, `kube-rbac-proxy` and `internal-tls-proxy` containers, runs the requested image, carries the resource's schedule and storage settings, and mounts a bound claim at the requested size; `/q/health` answers `healthy` unauthenticated; `/info` needs a token and refuses one without the grant; both halves of a drift comparison upload and are counted; CompareMeans detects the shifted distribution; and a scheduled metric is listed once and deleted |
| `tests/evalhub/test_evalhub.py` | EvalHub: the CRD serves `v1` and `v1alpha1` through a conversion webhook; the instance the tests create reads back through both versions, reports `Ready`, runs one pod with the `evalhub` and `kube-rbac-proxy` containers and the labels the Service selects on; a namespace labelled as a tenant receives the discovery and CA ConfigMaps; and `/api/v1/health` answers `200` (and `405` to non-GET methods) |

Two axes select what runs, and they compose: a **directory** names a component
(`tests/evalhub`), a **marker** names a section within it (`evalhub`, `benchmark`,
`adapters`, `generation`, `module`, `service`, `emulator`, `workload`).
Every module in a component directory carries that component's marker as well as its
own, so a section is both part of its component and runnable on its own.

```sh
make test                                                  # everything except the adapters
make test-evalhub                                          # all EvalHub checks, adapters excluded
make test-evalhub-generation                               # just the collection generation authorization
make test-evalhub-adapters                                 # just the adapter jobs (slow, own report)
make test-lmeval                                           # just the LMEvalJob checks
make test-all                                              # everything, adapters included
```

Anything else goes through `test-select`, which takes a marker expression, a
directory, a node id or `-k` - whatever pytest accepts:

```sh
make test-select SELECT='-m generation'                    # one section
make test-select SELECT=tests/evalhub                      # one component directory
make test-select SELECT=tests/lmeval                       # the same, for another component
make test-select SELECT='-m "evalhub and not adapters"'     # a component without its slow section
make test-select SELECT='-m "evalhub and not benchmark and not adapters"'
make test-select SELECT=tests/evalhub/test_evalhub_collection_generation.py
make test-select SELECT=tests/evalhub/test_evalhub.py::test_evalhub_health_endpoint
make test-select SELECT='-k tenant_role'                   # one check, by name
```

For example: `-m module` the modular stack, `-m workload` the workload operator,
`-m lmeval` an `LMEvalJob` and its NetworkPolicy, `-m service` the TrustyAI service,
`-m emulator` the LLM emulator, `-m benchmark` the harness smoke job. The same
selections run through `make test PYTEST_ARGS=...` when a full run with an extra
filter is what you want; `test-select` runs only the selection.

`-m evalhub` includes the adapter jobs, because they are EvalHub checks too; they
are excluded from `make test` and `make test-evalhub` by the `adapters` marker
rather than by not being marked. See [EvalHub adapters](#evalhub-adapters).

Checks that need an OLM-managed or platform-managed install are deliberately
absent, because the harness cannot provide one: the `odh-trustyai-config` and
`trustyai-dsc-config` ConfigMaps written by a platform operator, the check that
the image is listed in an operator CSV's `relatedImages` and released from
`registry.redhat.io` by digest, and the EvalHub provider, collection, MCP,
metrics and multi-tenancy suites, which need Kueue, MCP or the platform's tenant
RBAC. Of the providers, one job is covered: [EvalHub adapters](#evalhub-adapters)
runs `ethics_cm` of `lm_evaluation_harness` end to end, not the suite of them.

The EvalHub API checks call `/api/v1/health` through a `kubectl port-forward` to
the Service. That path is exempt from the kube-rbac-proxy authorization the
operator configures, so the health checks need no token; they do not verify the
certificate, because a forwarded port is reached as 127.0.0.1 and the harness
certificate only covers the in-cluster Service names.

```sh
make test PYTEST_ARGS="-v"                                 # extra pytest flags
KUBE_CONTEXT=my-cluster NAMESPACE=opendatahub make test     # another cluster
```

### Keeping the resources

By default the fixtures remove what they created. Set `KEEP_RESOURCES=1` - or run
`make test-keep`, or pass `--keep-resources` through `PYTEST_ARGS` - to leave it
all in place and look at it afterwards:

```sh
make test KEEP_RESOURCES=1
kubectl -n "$(kubectl get ns -o name | grep trustyai-evalhub | head -1 | cut -d/ -f2)" get all,cm,evalhubs,jobs
kubectl -n system get deploy,svc,cm,secret -l app=vllm-emulator
```

That keeps the EvalHub instance with its tenant namespace, the LLM emulator with
its Secret and wrapper ConfigMap, and the evaluation job with its spec ConfigMap,
its pod and the adapter logs. The run logs each namespace it leaves behind and the
HTML report says so too. Nothing is kept silently: with the default setting those
are removed at the end of the session.

The two operators - and the module resource `make up` applied - are not the
fixtures' to remove: they stay until `make down` deletes the cluster, or until you
delete the resource, which takes the workload operator and its operands with it.
A session that had to create the resource itself, because no `make deploy` had run
on that cluster, does delete it again, and a later run adopts whatever it finds
rather than replacing it.

> **Warning: kept resources affect later runs.** Every EvalHub instance is named
> `evalhub` and reconciles *every* tenant-labelled namespace, including namespaces
> it did not create, so a kept instance keeps writing into a later run's tenant
> namespace. The run grants its own job access, so a re-run still passes, but the
> namespaces pile up, the kept instance competes for the same tenant resources, and
> the emulator keeps its port in the namespace under test. Delete the leftovers
> before a run you want to be clean:
>
> ```sh
> kubectl get ns -o name | grep trustyai-evalhub | xargs -r kubectl delete ns
> kubectl -n system delete deploy,svc -l app=vllm-emulator
> kubectl -n system delete cm/vllm-emulator-auth-script secret/vllm-emulator-auth
> kubectl delete trustyais.components.platform.opendatahub.io default-trustyai
> ```
>
> The run repeats this warning at start-up whenever `KEEP_RESOURCES` is set.

Runs are verbose by design: at INFO level the suite logs the target context and
server version, the image each operator runs, replica and condition details, and
the readiness of every pod, so a passing run shows what was checked and a
failing one says what was not.

Every run also writes a self-contained HTML report (git-ignored) that keeps the
captured log and failure detail per test: `reports/report.html` for `make test`
and `make test-all`, `reports/adapters.html` for `make test-evalhub-adapters`.
The run is ordered by scope and the report shows it: one block each for the EvalHub
adapters, EvalHub, the LLM emulator, an `LMEvalJob`, the module operator, the
TrustyAI service and the workload operator, a `Scope` column naming the block its
first row starts, and a per-scope count above the table.

```sh
make test REPORT_FILE=/tmp/trustyai.html                     # choose the report path
make test-evalhub-adapters ADAPTERS_REPORT_FILE=/tmp/a.html  # the same for the adapters
```

Checks run against `KUBE_CONTEXT` (default `kind-trustyai`) and `NAMESPACE`
(default `system`), the values the harness uses by default. A missing kubeconfig
or context skips the suite; a missing Deployment fails with a pointer to
`make up`.

Layout:

- `tests/` — test modules, one file per area; `tests/conftest.py` holds the
  fixtures that own the operands: `trustyai_module` ensures the module resource is
  there and waits for the workload operator under it, the EvalHub instance and the
  LLM emulator are the session's, and `workload_namespaces`, `trustyai_services` and
  `evalhub_tenants` are factories a check uses for a workload of its own
- `tests/evalhub/` — the EvalHub sections in one directory: the instance and its
  health endpoint, the benchmark job, the adapters, and the collection generation
  authorization. They share the fixtures in `tests/conftest.py`, and
  `utilities/evalhub_auth.py` holds the policy and tenant-Role readers the
  authorization checks use
- `tests/lmeval/` — the `LMEvalJob` checks: the execution Pod the workload operator
  creates and the NetworkPolicy that selects it, repaired after drift, deletion and a
  lost identity label. `utilities/lmeval.py` holds the job manifest and the waits
- `utilities/` — helpers shared by the tests; `utilities/kube.py` loads the
  clients and turns Deployment and pod status into readable failures,
  `utilities/trustyai.py` builds the module resource and waits for the stack
  under it, `utilities/stack.py` runs those steps for `make deploy`,
  `utilities/trustyai_service.py` owns the whole `TrustyAIService` workload
  lifecycle, `utilities/portforward.py` hands out local ports for the forwards,
  `utilities/model.py` chooses the model endpoint (`MODEL_URL` and `MODEL_KEY`)
  and writes the credential Secret it resolves, and the other modules hold the
  per-component helpers
- `conftest.py` — session fixtures for the context, namespace and API clients
- `pyproject.toml` — the dependencies (`kubernetes`, `pytest`, `requests`,
  `cryptography`, `pyyaml`) with the `pytest-html` and `ruff` dev group, the marker
  registry, and the `ruff` configuration; `uv.lock` pins them and the make
  targets run them through `uv`

`make lint` runs `ruff check` and `ruff format --check`; `make fmt` fixes both.
`.github/workflows/python-lint.yaml` runs `make lint` in CI on pushes to
`main` and on pull requests.
