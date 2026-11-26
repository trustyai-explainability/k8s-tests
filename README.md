<p align="center">
  <img src="docs/logo-k8s-tests.png" alt="k8s-tests" width="220">
</p>

# k8s-tests

[![Python lint](https://github.com/trustyai-explainability/k8s-tests/actions/workflows/python-lint.yaml/badge.svg?branch=main)](https://github.com/trustyai-explainability/k8s-tests/actions/workflows/python-lint.yaml)

Kubernetes-level tests and harnesses for the TrustyAI components.

## Local Kind harness

`k8s-tests` brings up a local [Kind](https://kind.sigs.k8s.io/) cluster and runs the
TrustyAI module and workload operators on it: it builds both operator images from a
`trustyai-service-operator` checkout, loads them into the cluster, and deploys the
module operator with its image values overridden to the images it just built. The
module operator then renders and deploys the workload operator from the manifests
baked into its image.

The harness stops at the operators: `up` also creates the `TrustyAI` module resource,
which makes the module operator deploy the workload operator, and waits until both run
the images it built. The resource is built in Python rather than from a manifest, so no
YAML here has to be kept in step with the operator. What the checks then look at -
`TrustyAIService`, `EvalHub` instances, the jobs that call a model - is created by the
suite, the way a user would, so the operand lifecycle is under test.

The harness is a Python package with a console script; there is no shell script left in
it. Every knob is a flag and an environment variable of the same name, so
`k8s-tests up --operator-repo X` and `OPERATOR_REPO=X k8s-tests up` are the same run, and
`k8s-tests --help` documents both spellings.

### Prerequisites

- `kind` and `kubectl` 1.24 or newer, which the checks need to mint bearer tokens with
  `kubectl create token`
- `podman` or `docker` to build the images
- `uv`, which resolves the pinned Python 3.11+ dependencies from `uv.lock` for
  `uv run k8s-tests ...`
- a `trustyai-service-operator` checkout, passed as `--operator-repo`/`OPERATOR_REPO`.
  The TLS secrets Kind cannot issue are generated in-process (`cryptography` is a
  dependency)

### Quick start

```sh
k8s-tests up --operator-repo /path/to/trustyai-service-operator
k8s-tests status     # both operators, and no operands
k8s-tests test       # creates the operands and checks them
```

From a checkout, prefix with `uv run` (`uv run k8s-tests up ...`). `up` is the four
steps below, run in order; each is usable on its own:

```sh
k8s-tests cluster    # create the Kind cluster (idempotent)
k8s-tests images     # build the workload and module operator images from OPERATOR_REPO
k8s-tests load       # load both images into the cluster
k8s-tests deploy     # deploy the module operator, pointing it at the built images
```

`up`, `images`, `images-all` and `deploy` require `OPERATOR_REPO` and stop with a pointer
to the flag when it is missing or does not look like an operator checkout. `load` reads the
layout from that checkout to know how many images it loaded, so it takes `OPERATOR_REPO`
too, or `--operator-arch single|modular` when the checkout is not at hand; with neither it
stops rather than reading the layout out of the current directory.

Tear the cluster down with `k8s-tests down`. `k8s-tests clean` also deletes the images
the harness built, the local registry container `images-all` starts, and the HTML
reports (`reports/`, the `images.env` record included), without touching the cluster.
With podman or Docker stopped it still removes the reports and leaves the images and the
registry for a later run.

### Installing it

```sh
uv pip install .                 # into the active environment
uv tool install .                # or as a standalone command
k8s-tests test --section lmeval
```

The wheel carries the checks and their assets (`harness/data/`), so an installed copy
runs the whole suite from any directory, against whatever cluster `KUBE_CONTEXT` and
`NAMESPACE` name. `-v` reports which copy a run reads: the checkout when there is one,
the installed one otherwise. `lint` and `fmt` act on a checkout and need
`K8S_TESTS_ROOT=/path/to/k8s-tests` when the installation has none.

### Building every image locally (local registry)

`k8s-tests images-all` builds every image the suite can build from source and pushes it
to a `registry:2` container on `127.0.0.1:$REGISTRY_PORT` (5005). The Kind nodes pull
from it through a containerd mirror, so nothing is loaded into the node. It builds:

- the workload and module operator images from `OPERATOR_REPO`;
- the EvalHub service image from `EVALHUB_REPO`;
- every adapter image from `EVALHUB_CONTRIB_REPO` (`ADAPTERS=all`), or only those named;
- the `lm_evaluation_harness` adapter the adapter checks use, from
  `LMES_REPO/Dockerfile.lmes-job`.

The three repositories default to siblings of this checkout (`../eval-hub`,
`../eval-hub-contrib`, `../lm-evaluation-harness`).

```sh
# Build and push everything, then deploy with those images
k8s-tests images-all --operator-repo /path/to/trustyai-service-operator
k8s-tests deploy-registry --operator-repo /path/to/trustyai-service-operator

# Or in one step: cluster + images-all + deploy-registry
k8s-tests up-registry --operator-repo /path/to/trustyai-service-operator

# Only the adapter the adapter checks need (much faster than --adapters all)
k8s-tests up-registry --operator-repo /path/to/trustyai-service-operator --adapters lmes
```

`--adapters all` is the default and builds every eval-hub-contrib adapter plus
`community-lmes`; several are large (GuideLLM alone is about 7.5 GB), so `--adapters
lmes` or a list of adapter directory names is the quick path. `images-all` writes the
references to `reports/images.env`; `deploy-registry` reads them, drops the ones it does
not use, and marks the file as a record of what is running. The checks read that file
when the image variables are not set, so `k8s-tests test` after a registry deploy
compares against the images that were pushed - and an image that was built but never
deployed makes it compare against nothing.

`deploy-registry` and `up-registry` accept the same image flags as `deploy`/`up`. A flag
or variable given there wins over the record, and the references the deploy does not use
are dropped from the file so the checks keep comparing against the ones it does.

How the images reach the cluster:

- `images-all` starts the registry on the `kind` container network, publishes it on
  `127.0.0.1:$REGISTRY_PORT`, and writes
  `/etc/containerd/certs.d/localhost:$REGISTRY_PORT/hosts.toml` on every node. Pods that
  reference `localhost:$REGISTRY_PORT/...` pull from it over HTTP.
- New clusters get the certs.d config path from `hack/kind/cluster.yaml`. A cluster
  created before that patch has it enabled in place; `--patch-containerd 0` fails
  instead, and asks for a recreated cluster (`k8s-tests down`).
- The build engine is the one that owns the Kind cluster (its node containers decide,
  and `KIND_PROVIDER` overrides that); with no cluster yet podman is preferred. podman
  pushes to the plain-HTTP registry with `--tls-verify=false`. With Docker the daemon
  must allow the local registry (`insecure-registries`) or the push fails with `server
  gave HTTP response to HTTPS client`.
- The registry container is reused between runs; `clean` removes it, so everything it
  held is gone - including an image pushed by hand, such as the kube-rbac-proxy build in
  Troubleshooting, which has to be pushed again before the next `deploy`. It also
  removes the local copies the record names, and leaves images under the registry prefix
  that the record does not name alone.
- On a small podman machine the adapter images fill the VM quickly. `--remove-after-push`
  drops each local copy once it is pushed (the registry keeps it), and `--adapters lmes`
  is cheaper than all of them (GuideLLM is ~7.5 GB; LightEval and MTEB are larger).

### What `k8s-tests deploy` does

1. Creates the target namespace (`system` by default).
2. Applies the external CRD stand-ins the operators expect, copied from
   `$OPERATOR_REPO/tests/crds`: OpenShift `Route` and Prometheus Operator
   `Prometheus`/`ServiceMonitor`, plus KServe `InferenceService` for a run whose operators
   still watch it (3.6 and earlier - see [The serving API is
   optional](#the-serving-api-is-optional)).
3. Renders `$OPERATOR_REPO/trustyai-operator-module/config/default` from a temporary
   copy, so the module operator image, the workload operator image it renders
   (`RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_OPERATOR_IMAGE`), the operand images the
   workload operator resolves (`RELATED_IMAGE_ODH_*`, from `--evalhub-img`,
   `--trustyai-service-img`, `--kube-rbac-proxy-img`, `--lmes-img`, `--inspect-img`,
   `--lighteval-img`) and the namespace are overridden without touching the checkout.
4. Waits for the module operator to come up, seeds the `Prometheus` instance it expects,
   and creates the webhook and metrics TLS secrets that OpenShift's service CA would
   normally issue (Kind has no service CA).
5. Applies the `TrustyAI` module resource through `utilities/stack.py`, which builds it
   in Python - there is no manifest - and waits for the workload operator the module
   operator deploys from it: the Deployment, the image it runs and the `RELATED_IMAGE_*`
   values it resolves, and its rollout.
6. Points the `TrustyAIService` and `EvalHub` CRD conversion webhooks at the seeded CA.

That is where the harness stops: both operators run, with the images this run asked for,
and nothing is reconciled on top of them. `k8s-tests status` right after `up` shows the
two operators and no operands.

### The module resource and the operands

The module resource is built by `utilities/trustyai.py:module_manifest`:
`default-trustyai`, `managementState: Managed`, with TAS, LMES, EvalHub and NeMo
Guardrails enabled. `k8s-tests deploy` applies it through `utilities/stack.py`; the
`trustyai_module` fixture in `tests/conftest.py` then adopts it rather than rewriting it,
and only creates it when the cluster has none - a cluster brought up without `deploy`,
which is also the only case where the session deletes it again (unless `KEEP_RESOURCES`
is set). A resource that is already there, on a platform-managed install for instance,
is never touched.

Those four are the harness's *capability*, not a list of what is covered: the module
resource is a cluster-scoped singleton whose `enabledServices` becomes the workload
operator's `--enable-services` argument, with every controller in that one manager
process, so a service can only be switched on here. All four are on so a check can create
whatever operand it needs, in a namespace of its own - the way `opendatahub-tests` does -
rather than because a check exists for each today.

Everything on top of the operators is created by the suite, the way a user or the
platform operator would, so there is no manifest for any of it:

- an `EvalHub` instance with its namespace and TLS material, and a `TrustyAIService`,
  each in its own namespace from a fixture in `tests/conftest.py`;
- the LLM emulator, or the model credential Secret when `MODEL_URL`/`MODEL_KEY` name a
  real endpoint, and the resources a job needs in its tenant namespace.

The fixtures depend on `trustyai_module`, so no operand is created before the workload
operator that reconciles it is up; the fixture repeats the waits `utilities/stack.py`
does, which is what makes a bare `k8s-tests test` - without a fresh `deploy` - wait for a
stack someone else brought up instead of racing it.

### Bringing a workload of your own

The operators are the session's, a workload is the check's. Three fixtures make that the
default shape of a new section, and each removes what it created when the check that
asked for it finishes:

| Fixture | Gives you |
| --- | --- |
| `workload_namespaces(request, prefix=...)` | a namespace for a resource you bring yourself |
| `trustyai_services(request)` | a `TrustyAIService` in a namespace of its own, waited for, with port-forwards to its API (pass `namespace=` to reuse one) |
| `evalhub_tenants(request)` | a tenant-labelled namespace for an EvalHub job |

So a section that needs its own copy of something asks for one, in its own namespace,
instead of sharing the session's:

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
`TRUSTYAI_SERVICE_API_PORT`/`TRUSTYAI_SERVICE_HEALTH_PORT` (18443/18080), and give them
back at teardown. What a check cannot have of its own is a service that is not enabled on
the module resource: `enabledServices` is the one cluster-wide switch, so `LMEvalJob`,
`EvalHub` and guardrails workloads work because all four are on. The `LMEvalJob` shape
above is the operator's own sample (`config/samples/lmes_v1alpha1_lmevaljob_dra.yaml`),
minus the DRA resource claim; a marker of your own belongs in `pyproject.toml` next to
the others.

### Settings

Every one is a flag and an environment variable; `k8s-tests --help` lists them all.

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPERATOR_REPO` | *(required)* | Checkout the images and manifests come from |
| `OPERATOR_ARCH` | *(from the checkout)* | `modular` (3.6+, module operator) or `single` (3.5 and earlier); naming a layout the checkout does not have is an error |
| `OPERATOR_RELEASE` | *(empty)* | RHOAI/ODH release that checkout is from (`3.6`, `3.7`), as `--release`; gates the checks by release, see [Release gating](#release-gating), and decides whether the KServe `InferenceService` CRD is installed |
| `SERVING_CRD` | `auto` | `auto` installs the `InferenceService` CRD only for a 3.6 or earlier run, by `OPERATOR_RELEASE` or else by a pre-module checkout; `always`/`never` override, as `--serving-crd` |
| `KIND_CLUSTER` | `trustyai` | Kind cluster name (context `kind-<name>`) |
| `KIND_NODE_IMAGE` | Kind's default | Pin a node image, e.g. `kindest/node:v1.29.0` |
| `KIND_PROVIDER` | auto | Force `podman` or `docker` on hosts that have both |
| `NAMESPACE` | `system` | Namespace both operators are deployed into |
| `KUBECTL` | `kubectl` | kubectl the harness and the checks shell out to, including the token the API checks mint |
| `MODEL_URL` | *(empty)* | OpenAI-compatible base URL the job checks call instead of the emulator; setting it stops the fixtures deploying the emulator |
| `MODEL_KEY` | *(empty)* | Credential for `MODEL_URL`; the fixtures write it to the `model-credentials` Secret under `api-key`, which the job names in `model.auth.secret_ref` |
| `MODEL_NAME` | `TinyLlama/TinyLlama-1.1B-Chat-v1.0` | Model name a job submits |
| `WORKLOAD_IMG` | `localhost/trustyai-service-operator:dev` | Workload operator image reference |
| `MODULE_IMG` | `localhost/trustyai-operator-module:dev` | Module operator image reference |
| `ATIF_IMG` | *(empty)* | ATIF adapter image the adapters-atif section points EvalHub's provider at; empty keeps the operator's pin |
| `MLFLOW_IMG` | *(empty)* | MLflow tracking server the adapters-atif section deploys; empty uses `ghcr.io/mlflow/mlflow:v3.1.0` |
| `EVALHUB_IMG` | *(empty)* | EvalHub operand image; empty keeps the operator's own pin (`quay.io/evalhub/evalhub:latest`) |
| `TRUSTYAI_SERVICE_IMG` | `quay.io/trustyai/trustyai-service:latest` | TrustyAI service operand image, passed through `RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_PY_IMAGE`; set it to a build under test |
| `KUBE_RBAC_PROXY_IMG` | *(empty)* | kube-rbac-proxy sidecar image, passed through `RELATED_IMAGE_ODH_KUBE_RBAC_PROXY_IMAGE`; set it to an arm64 build of `opendatahub-io/kube-rbac-proxy` on Apple Silicon (see Troubleshooting) |
| `TAG` | `dev` | Tag used by both image references |
| `IMAGE_PREFIX` | `localhost` | Registry/repository prefix used by both image references |
| `REGISTRY_PORT` | `5005` | Host port of the registry `images-all` starts on the `kind` network |
| `EVALHUB_REPO` | `../eval-hub` | Checkout `images-all` builds the EvalHub service image from |
| `EVALHUB_CONTRIB_REPO` | `../eval-hub-contrib` | Checkout `images-all` builds the adapter images from |
| `LMES_REPO` | `../lm-evaluation-harness` | Checkout `images-all` builds `community-lmes` from (`Dockerfile.lmes-job`) |
| `ADAPTERS` | `all` | `all`, `lmes`, or a comma-separated list of eval-hub-contrib adapter directories |
| `LMES_IMG` / `INSPECT_IMG` / `LIGHTEVAL_IMG` | *(empty)* | Adapter image overrides passed through the module operator (`RELATED_IMAGE_ODH_*`) |
| `GARAK_IMG` | *(empty)* | garak adapter image, passed through `RELATED_IMAGE_ODH_TRUSTYAI_GARAK_LLS_PROVIDER_DSP_IMAGE` (`garak-provider-image` on a pre-module operator). Set it to test a build of the adapter, for example one loaded with `kind load image-archive`. Operators before trustyai-explainability/trustyai-service-operator#1145 (3.5 included) pin an image that needs credentials to pull, so garak's adapters run needs this set against them |
| `PLATFORM` | engine host platform | Platform the images are built for (`linux/arm64` on Apple Silicon) |
| `IMAGES_ENV_FILE` | `reports/images.env` | Where `images-all` writes the image references and `deploy` and the checks read them |
| `REMOVE_AFTER_PUSH` | *(empty)* | `images-all` removes each local image after pushing it; frees the podman machine's disk (incompatible with `SKIP_BUILD`) |
| `SKIP_BUILD` | *(empty)* | `images-all` pushes the images already in the engine's store |
| `PATCH_CONTAINERD` | `1` | Enable the certs.d config path in place on a cluster created before `hack/kind/cluster.yaml` had it |
| `ENGINE` | auto | The engine that owns the Kind cluster (podman when there is no cluster yet); set it to force one |
| `TIMEOUT` | `300s` | Timeout for each readiness wait |
| `PYTEST_ARGS` | *(empty)* | Extra pytest arguments, e.g. `-m module`, `-m lmeval`, `-m adapters` or `-k evalhub` |
| `REPORT_FILE` | `reports/report.html` | Report written by `k8s-tests test` (default and `--section all`) |
| `ADAPTERS_REPORT_FILE` | `reports/adapters.html` | Report written by `--section adapters` |
| `KEEP_RESOURCES` | *(empty)* | Leave everything the run created on the cluster: see [Keeping the resources](#keeping-the-resources) |

The LLM emulator's `EMULATOR_IMG`, `EMULATOR_NAMESPACE` and `EMULATOR_TOKEN` are
documented under [LLM emulator](#llm-emulator).

### Operator layouts

There are two, and the checkout decides which one a run deploys — `trustyai-operator-module/`
present means the module operator exists and renders the workload operator; absent means
one operator, deployed directly (3.5 and earlier).

```console
$ k8s-tests check-operator-repo --operator-repo /tmp/operator-rhoai-3.5
==> /tmp/operator-rhoai-3.5 is a trustyai-service-operator checkout
==> layout: single (the checkout, which has no trustyai-operator-module/ (pre-module))
```

What differs, and nothing else does:

- `images` builds one operator image instead of two, and `images-all` pushes one instead of
  two (`MODULE_IMG` is not recorded);
- `deploy` renders the checkout's own overlay (`config/overlays/rhoai`, then `odh`, then
  `default`) and patches `trustyai-service-operator-config` with the operator and operand
  images, instead of deploying the module operator and its module resource;
- the Prometheus CRD stand-in and the module resource are skipped, since only the module
  operator needs them.

`--operator-arch` asserts a layout rather than choosing one: on a checkout that lacks it
the run stops with what is missing, which is how a half-synced checkout gets caught before
an image is built. `--release` stays the release *under test* — it gates the checks and
never overrides what the tree can deploy; a run where the two disagree says so.

On a single cluster the module operator's rows in `tests/test_trustyai_operators.py` skip
because the Deployment is not there, and `tests/test_trustyai_workload.py` skips the
override check for the same reason.

**The EvalHub images need the same generation as the operator.** A rhoai-3.5 checkout
pins `evalhub:latest` and `ta-lmes-job:latest` in its `params.env` — moving tags. Today's
EvalHub 1.0 service creates a NetworkPolicy per job (eval-hub #1070), which that operator's
`evalhub-jobs-writer` ClusterRole does not allow (`batch/jobs` only), and the current
`ta-lmes-job` no longer speaks the job contract the 3.5 service drives. Both fail, from the
service rather than the checks, so a 3.5 run that wants the EvalHub sections names that
release's GA images — which `deploy` prints a reminder for:

```sh
k8s-tests up --operator-repo /path/to/rhoai-3.5-checkout \
  --evalhub-img quay.io/opendatahub/odh-eval-hub:odh-3.5 \
  --lmes-img quay.io/opendatahub/ta-lmes-job:odh-3.5-ga
```

With that pair the benchmark section passes on a 3.5 cluster; the checks themselves carry
no release bound, because nothing about them is 3.6-specific.

### Release gating

The component does not look the same in every release, so a check can declare the ones
it applies to:

```python
from utilities.release import release


@release(min="3.6")
def test_trustyai_module_crd_exists(api_client: client.ApiClient) -> None: ...
```

Declare the release under test with `--release` (the variable is `OPERATOR_RELEASE`, so it
cannot be confused with the release of something else in the environment) and the session
skips what falls outside it, with the range and the release in the skip reason:

```sh
k8s-tests test --release 3.5                     # or OPERATOR_RELEASE=3.5
k8s-tests test --release 3.6 --select tests/test_trustyai_module.py
OPERATOR_RELEASE=3.5 pytest -rs -m lmeval        # the same, without the harness
```

Bounds are inclusive and compared component by component, so `max="3.6"` covers 3.6.0
but not 3.6.1, and `3.6-ea.2` compares as `3.6`. With no release declared nothing is
gated: the checks run and the fixtures skip whatever the cluster does not have, which is
how a bare `pytest` against a cluster somebody else deployed still works. The declared
release is logged at the start of a run and printed in the HTML report.

The ranges the checks declare today: the module stack (`tests/test_trustyai_module.py`,
the module operator's row in `tests/test_trustyai_operators.py`, and the workload
override check) needs the operator split that landed in 3.6, and so does the
NetworkPolicy an `LMEvalJob` runs under (`tests/lmeval/`). The install with no serving
API (`tests/test_trustyai_serving_optional.py`) needs 3.7, the first release whose module
operator reports an absent API as an observation; a 3.6 run skips those checks and keeps
installing the CRD.

### Notes and troubleshooting

- `images-all` needs the local registry to be reachable from the nodes. If a pod stays in
  `ImagePullBackOff` for a `localhost:$REGISTRY_PORT/...` image, check
  `curl http://127.0.0.1:$REGISTRY_PORT/v2/_catalog`, that the
  `k8s-tests-registry-<cluster>` container is running, and that
  `/etc/containerd/certs.d/localhost:$REGISTRY_PORT/hosts.toml` exists in the node
  (`podman exec <node> cat ...`).
- With Docker as the engine, pushing to the plain-HTTP registry fails with `server gave
  HTTP response to HTTPS client` unless the daemon lists it under
  `insecure-registries`. The engine follows the cluster's provider, so a podman-backed
  cluster (`KIND_PROVIDER=podman`) is the way to use podman's push path; `ENGINE=podman`
  overrides the build engine when both are installed.
- Creating a new Kind cluster on a rootless podman machine needs `Delegate=yes` in the
  machine's systemd user slice (see the
  [kind rootless documentation](https://kind.sigs.k8s.io/docs/user/rootless/)).
  `images-all` can enable the containerd registry config path on an existing cluster in
  place, so a cluster created before `hack/kind/cluster.yaml` had the patch still works.
- A build that fails with `no space left on device` is the podman machine's own disk, not
  the Mac's. Check it with `podman system df` and `podman machine ssh 'df -h /'`.
  `podman image prune -a -f` and `podman builder prune -f` reclaim unused images and
  build cache, `kind delete cluster --name <old-cluster>` frees the volumes of a cluster
  you no longer need, and `podman machine set --disk-size 300` grows the disk (it stops
  the machine first, so the clusters stop too).
- `up` leaves both operators running and no operands, so `status` shows the module
  operator, the workload operator and no `EvalHub` instance. If the workload operator
  never appears, the module operator could not reconcile the module resource: `logs`
  tails the module operator, and
  `kubectl get trustyais.components.platform.opendatahub.io default-trustyai -o yaml`
  shows the conditions it reported.
- `deploy` converges the module resource on the services the harness needs, so re-running
  it resets one that was edited by hand. A test session never does that: it adopts the
  resource it finds.
- Image tags other than `latest` are important: `latest` would make Kubernetes pull
  instead of using the image loaded into the node.
- The operand images the operator pins are published for `linux/amd64` only:
  `quay.io/opendatahub/odh-kube-rbac-proxy:odh-stable`, and `ta-lmes-job` and
  `ta-lmes-driver` in the same overlay. On an arm64 host, such as an Apple Silicon
  laptop, the operand pods stay in `ImagePullBackOff` (`no match for platform in
  manifest`) and the rollout wait fails with the pod's reason in the timeout message.
  Point the run at an amd64 cluster, or build the proxy for the host platform from
  `opendatahub-io/kube-rbac-proxy` and pass it to the module operator, which renders it
  into the workload operator:

  ```bash
  git clone --depth 1 https://github.com/opendatahub-io/kube-rbac-proxy /tmp/kube-rbac-proxy
  cd /tmp/kube-rbac-proxy
  GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -o _output/kube-rbac-proxy-linux-arm64 \
      ./cmd/kube-rbac-proxy
  podman build --platform linux/arm64 \
      --build-arg BINARY=kube-rbac-proxy-linux-arm64 --build-arg GOARCH=arm64 \
      --build-arg BASEIMAGE=gcr.io/distroless/static:nonroot-arm64 \
      -t localhost:5005/odh-kube-rbac-proxy:dev .
  podman push --tls-verify=false localhost:5005/odh-kube-rbac-proxy:dev
  ```

  `docker` works too, but needs `--load` on the build (buildx's `docker-container`
  driver otherwise leaves the image only in its cache) and `localhost:5005` in the
  daemon's `insecure-registries` for the push; podman, what a podman-backed Kind cluster
  uses, needs neither.

  then deploy with `--kube-rbac-proxy-img localhost:5005/odh-kube-rbac-proxy:dev`. The
  variable becomes the module operator's `RELATED_IMAGE_ODH_KUBE_RBAC_PROXY_IMAGE`, and
  `deploy` waits until the workload operator renders it. The upstream
  `quay.io/brancz/kube-rbac-proxy` is multi-arch but lacks the ODH
  `--tls-curve-preferences` flag, so it cannot stand in for it.
- Any `LMEvalJob` on the cluster keeps the module resource `Ready=False`
  (`LMES: ... operand status is not yet available`): the module operator's operand health
  check reads `status.conditions[type=Ready]` and `status.phase`, while an `LMEvalJob`
  reports success as `status.state: Complete`. That includes the deliberately
  unschedulable job the `lmeval` checks create, so a `KEEP_RESOURCES` run leaves the
  module not ready for every later run. Delete the leftover namespaces (`kubectl get ns -o
  name | grep -E 'trustyai-lmeval|trustyai-evalhub|trustyai-service' | xargs -r kubectl
  delete`) before re-running, or expect that one condition to stay in the failure message
  until the job's finalizer has run.
- On hosts without the `docker` CLI the harness exports each image with `podman save` and
  loads it with `kind load image-archive`, since `kind load docker-image` always shells
  out to `docker`.
- `images` passes `--load` to `docker build`. The `docker` CLI builds with buildx, and
  when the active builder uses the `docker-container` driver a build without `--load`
  prints `No output specified with docker-container driver` and leaves the image only in
  the builder's cache, so `load` fails with `Error response from daemon:
  localhost/trustyai-service-operator:dev: image not known`. `ENGINE=podman` sidesteps
  buildx and its builder configuration altogether.
- The module image bakes the workload operator manifests at build time. If `config/` and
  `trustyai-operator-module/config/manifests-template/` have drifted, `images` warns;
  refresh the template with `make -C $OPERATOR_REPO sync-trustyai-module-manifests`.
- `logs` tails both operators in the namespace (`-l control-plane=controller-manager`);
  EvalHub runs as `-l app=eval-hub`.
- Every instance is named `evalhub` and reconciles every tenant-labelled namespace,
  including namespaces it did not create, so a `KEEP_RESOURCES` run or an interrupted one
  leaves an instance that keeps writing to later runs' namespaces. The run grants its own
  job access under a name the operator does not manage, so it does not depend on who
  reconciled last, and it logs a warning when it sees more than one instance. Delete what
  you do not need (`kubectl get evalhubs -A`).
- The LLM emulator (`vllm-emulator`) is deployed by the fixtures into the namespace under
  test and removed when the run ends; a job reaches it in-cluster as `vllm-emulator:8000`,
  which is what the benchmark and adapter checks submit. While a run is in flight it can
  be poked at from the host with `kubectl -n system port-forward svc/vllm-emulator 8000`.
- The EvalHub tests create their own instance with the sqlite database backend, since Kind
  has no PostgreSQL, and mint its `evalhub-tls` certificate and `evalhub-service-ca`
  bundle, which OpenShift's service CA would provide. The EvalHub image is pulled from the
  registry rather than loaded into the node, so the first `k8s-tests test` needs network
  access; the adapter checks also fetch their benchmark's dataset from the Hub.
  `k8s-tests up --evalhub-img <ref>` points the instances those tests create at another
  image, for example one built from `eval-hub`.

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
k8s-tests test --model-url http://model.example.svc:8000/v1 --model-key sk-...
k8s-tests test --model-url http://model.example.svc:8000/v1 --model-name granite-3b
```

Without `MODEL_URL`, `MODEL_KEY` is also the token the emulator requires, so the
credential plumbing can be exercised with a key of your own.

The tests own the emulator. `tests/conftest.py` deploys it into the namespace
under test (and removes it afterwards), so `k8s-tests up` leaves no emulator behind
and a run cannot depend on one someone left in the cluster. The image, namespace,
port and token are parameters:

```sh
k8s-tests test                             # default emulator, namespace and token
k8s-tests test --emulator-namespace my-ns  # somewhere else
k8s-tests test --emulator-img quay.io/.../vllm_emulator:<tag>
k8s-tests test --emulator-token another-token
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
credential, so it stays in `k8s-tests test`. Benchmarks that are the point of the run
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
k8s-tests test --pytest-args "-m 'not benchmark'"
```

## EvalHub adapters

`tests/evalhub/test_evalhub_adapters.py` is the section that runs real benchmarks. Every
entry in its `BENCHMARKS` table is one adapter run: submitted through the API,
waited for until the adapter finishes in the cluster, and read back as a result.
It covers two today: `ethics_cm` (commonsense moral judgment) of the
`lm_evaluation_harness` provider, and `quick` (one DAN probe) of the `garak` provider,
each with `num_examples: 1`. Adding a benchmark is one more entry in that table, and
every check then runs once for it, under a `[<provider>-<benchmark>]` id, so
`--pytest-args "-k garak"` runs one provider's entries alone. Its jobs call the same endpoint the benchmark job does: the
emulator the fixtures deploy, or `MODEL_URL` when that is set, with `MODEL_KEY` as
the credential.

For each benchmark the section asserts that the job is accepted with exactly that
benchmark, its provider and its `num_examples`; that the job and the benchmark
alone reach `completed`; that `results.benchmarks[]` carries the benchmark, its
provider and numeric metrics; that the metric the provider config scores it on
(`acc` for `ethics_cm`, `attack_success_rate` for `quick`) is among those metrics and is a rate; that the provider's
pass criteria turn the score into a `test` block; and, when the model is the
emulator, that it authenticated the job's model requests, so the model was called
rather than the job merely reporting success.

The section carries `evalhub` and `adapters`: it is part of the EvalHub selection,
and `adapters` names the section within it. Because an adapter run costs minutes
and fetches its benchmark's dataset from the Hub, it is the one section the default
run excludes - `k8s-tests test` is `-m 'not adapters'` and `--section evalhub` is
`-m 'evalhub and not adapters'` - so it is not selected by accident, only by name:

```sh
k8s-tests test --section adapters             # only this section, into reports/adapters.html
k8s-tests test --section adapters --keep-resources   # leave the job, its pod and the logs behind
k8s-tests test --section all                  # every section, the adapters included
k8s-tests test --select '-m adapters'         # the same selection, into the usual report
```

## ATIF adapter: manifests, provenance, unscored jobs and version range

`tests/evalhub/test_evalhub_atif.py` is its own section because it has to build the
world the adapter reads: it deploys an MLflow tracking server, tells the EvalHub
instance about it, deploys a stub judge, generates NeMo Gym-shaped ATIF exports, and
only then submits jobs. It pins the behaviour RHAI-22310 defines, per check:

| What the check pins | What it asserts |
| --- | --- |
| Manifest joining (`atif_manifest_entry_count`, `atif_trajectory_count`) | the manifest is authoritative: the entry count is the manifest's, the trajectory count is what scored, and a file the manifest does not list is not loaded |
| Provenance (AC3) | the run's `atif/evaluation_results.json` carries the manifest's task/rollout indices, `digest_verified: true`, and the producer's declared `extra` object verbatim |
| Digest mismatch under `fail_fast` | the job fails naming the file whose bytes do not match the digest its manifest declares |
| Digest mismatch under `skip_failed_trajectory` | the job completes, the entry contributes no score, and no `atif_overall_score` is invented |
| Unscored reporting (AC4) | with every judge call failing, the job completes, publishes no `atif_overall_score`, and reports `atif_complete: false` with its counts |
| Empty manifest (AC5) | under `skip_invalid` the job completes with the invalid manifest counted, rather than crashing |
| Version range (AC7) | an `ATIF-v1.6` export still scores, an `ATIF-v1.9` export fails naming `ATIF-v1.0 through ATIF-v1.8` |
| Declared-field policy (AC3) | a root field the ATIF model does not declare is invalid input (`invalid_schema`), not silently dropped |

The export is generated, not downloaded: `utilities/atif.py` writes ATIF documents and
a `manifest.jsonl` whose rows carry `trajectory_path`, `_ng_task_index`,
`_ng_rollout_index` and `expected_sha256`, which is the shape `gym eval export atif`
produces. A one-shot Pod copies them from a ConfigMap onto a PersistentVolumeClaim,
and the job names that claim as its `test_data_ref`, so the adapter mounts it at
`/test_data` the way the platform mounts real pre-recorded data.

MLflow matters here because the adapter requires an `experiment_name` and writes its
report there: without a tracking server the API refuses a job that names an
experiment, and the adapter has nowhere to put the per-node provenance above. The
section deploys `ghcr.io/mlflow/mlflow` (override with `MLFLOW_IMG`), patches the
instance's `spec.env` with `MLFLOW_TRACKING_URI`, waits for the rollout, and creates
the experiment through the job's `experiment.name`. Artifacts are read back over a
forwarded port, through the same artifact API the SDK writes to.

`ATIF_IMG` says which adapter image the section is testing. It cannot be changed on a
running cluster: the provider ConfigMaps are rendered from the template baked into the
module operator image (`config/manifests-template`), and the operator re-applies them on
every reconcile, so a patch to the ConfigMap is reverted - and unlike inspect and
lighteval, `evalhub-provider-atif-image` has no `RELATED_IMAGE_*` variable for the
platform to fill. The harness therefore fixes the value when it builds that image:
setting `ATIF_IMG` for any command that builds and deploys stages a patched copy of
`trustyai-operator-module` in a temp directory, rewrites `params.env` (and the provider's
`image_pull_policy`, when the image is one you loaded rather than pushed) and builds from
it. Nothing is written to your checkout and nothing is edited on the cluster, and the
value survives a restart because it is part of the image the operator starts from. The
fixture then verifies that the provider really runs that image and fails with the
remediation rather than quietly exercising the pin; `ATIF_ALLOW_PINNED_IMAGE=1` tests
the pin on purpose.

An adapter whose image the module operator already resolves from a `RELATED_IMAGE_*`
variable is left alone: for those, setting the variable is the override and there is
nothing to stage.

On the load path the adapter image itself is part of the run: naming one (`--atif-img`,
`--inspect-img`, ...) makes `up` build it from `EVALHUB_CONTRIB_REPO` and load it onto the
node alongside the operators, so no registry and no separate `podman build` / `kind load`
is involved. `images` and `load` do the same two halves on their own. Any other image a run
names (`--evalhub-img`, `--trustyai-service-img`, `--kube-rbac-proxy-img`, `--lmes-img`,
`--mlflow-img`, `--emulator-img`) is loaded when the engine's store holds it, and left for
the cluster to pull when it does not - a remote reference needs no help from here.

The image is named once. `deploy` writes what it brought up into `reports/images.env` and
stamps it, and `test` fills the images it did not name from that record - so
`up --atif-img X` followed by a plain `test --section adapters-atif` checks the image that
build produced, rather than the operator's pin. Naming an image on `test` still wins over
the record, which is what makes testing a different image deliberate.

Two things this section has to work around on Kind, both of which are deployment
facts rather than adapter behaviour. Its instance is created without a provider list
that includes `atif`, so the fixture patches `spec.providers` - the operator only
copies a provider's ConfigMap into the instance namespace for a provider the instance
names, and EvalHub only serves what it has a ConfigMap for. And EvalHub parses
`MLFLOW_CA_CERT_PATH` as soon as it has a tracking URI: Kind has no service-serving
CA, so the operator's merged bundle is empty and the Pod exits with "file contains no
valid PEM certificates" until the instance is given `spec.mlflow.tls.caBundle`. The
fixture seeds that bundle and patches providers, the tracking URI and the CA in one
patch, then waits for the completed rollout - not merely an available Pod, which an
old revision keeps reporting while the replacement crash-loops.

The operator this section runs must be one whose TrustyAIService controller does not
watch `serving.kserve.io` (trustyai-service-operator#1124/#1127): on a checkout that
still watches it, the harness installs no `InferenceService` CRD stand-in - it does
that only for a pre-module layout or a declared 3.6-or-older release - and the operator
crash-loops on its cache sync, which makes every check here time out. `run-atif-section.sh`
builds from the reference checkout for that reason; a checkout that still watches the kind
needs `--serving-crd always`.

The section carries `evalhub`, `adapters` and `atif`. `atif` selects it on its own, into
its own report; `--section adapters` includes it with the other adapters, so narrowing with
`-k atif` reaches it the way `-k <adapter>` reaches every other adapter. A run that wants
the adapters without the slow block narrows the other way: `--pytest-args "-k 'not atif'"`.
It is slow - each job pulls the adapter image, runs judge calls and writes to MLflow - and
it is opt-in only:

```sh
k8s-tests --adapters atif --atif-img localhost/evalhub/community-atif:dev up   # build + load it into Kind, no registry
k8s-tests test --section adapters-atif                              # reads that image back from the record
k8s-tests --adapters atif images-all && k8s-tests deploy-registry   # build and push this adapter, recording ATIF_IMG
k8s-tests test --section adapters-atif                              # only this section, into reports/adapters-atif.html
k8s-tests test --select '-m atif' --keep-resources                  # leave the jobs and their pods behind
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
k8s-tests test --section generation           # this section only
k8s-tests test --select '-m generation'       # the same, spelled as a selection
k8s-tests test --select '-k tenant_role'      # or one check of it, by name
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
k8s-tests test --section lmeval    # only this section
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
`RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_PY_IMAGE`, and `k8s-tests test` asserts the
operand runs exactly that image:

```sh
k8s-tests up --operator-repo ... --trustyai-service-img quay.io/you/trustyai-service@sha256:...
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

## The serving API is optional

TrustyAI integrates with KServe: where `serving.kserve.io/v1beta1` `InferenceService`
objects exist, payload capture reads a predictor and points its logger at the TrustyAI
service. That integration is optional, and since 3.7 neither operator watches the kind any
more - the module operator asks the API server whether it is served, and the workload
operator reads the CRD through an uncached reader when it wants the schema
(`trustyai-service-operator#1136` and `#1137`). A cluster that never installs the CRD is a
supported install, not a broken one.

What the two operators report about it from 3.7 on:

| Resource | Condition | On a cluster with no serving API |
| --- | --- | --- |
| `TrustyAI/default-trustyai`, reported by the module operator | `KServeAvailable` | `False` / `NotInstalled`, severity `Info`, with a message naming `InferenceService.serving.kserve.io` |
| `TrustyAIService`, reported by the workload operator | `InferenceServicesPresent` | `False` / `ServingCRDNotFound`, with a message naming the missing CRD; `Ready` is left where it was |

Neither is a precondition: `DependenciesAvailable` still carries the required Prometheus
check and still stops reconciliation when it fails, while the serving condition only states
a fact. Up to 3.6 the same absence came out as `KServeAvailable=False` with reason
`PreConditionFailed`, which reads as an unmet requirement - keeping that wording out is
what the checks in this section are for.

### The CRD follows the release

`k8s-tests deploy` installs the `InferenceService` stand-in from `$OPERATOR_REPO/tests/crds`
only when the operators still watch the kind. `--release` decides when it is declared, and
the release *train* is what counts, because a 3.6.1 patch build carries the 3.6 watch; with
no release declared the layout decides, since a pre-module checkout is 3.5 or earlier and a
modular one without a declared release is taken to be the code the removal ships in.
`--serving-crd always|never` (`SERVING_CRD`) overrides both:

```sh
k8s-tests up --release 3.7            # no InferenceService CRD; the 3.7 path
k8s-tests up --release 3.6            # CRD installed, because a 3.6 operator watches it
k8s-tests up --serving-crd never      # a 3.6 checkout deployed without the API on purpose
```

The CRD is only needed at startup: controller-runtime lists and then watches, and an
informer for a kind the API server does not serve never syncs, so an operator that still
watches `InferenceService` never becomes ready on a cluster without it - which is why the
grant and the watch were removed together. A run that does not need the CRD but finds one
already on the cluster says so, because a CRD outlives the run that created it and the
checks below would otherwise skip quietly.

### What `--section isvc-free` asserts

`tests/test_trustyai_serving_optional.py` describes that install, and needs it: the fixture
skips the module when `inferenceservices.serving.kserve.io` is installed, so the section is
only meaningful on a cluster that has never had it (a fresh `k8s-tests up --release 3.7`, or
an existing one after `kubectl delete crd inferenceservices.serving.kserve.io`).

- the module reports `KServeAvailable=False` / `NotInstalled` / `Info`, names the API in its
  message, and has reconciled the current generation;
- no condition reports `PreConditionFailed`, and `Ready=True` / `Degraded=False` all the
  same - an optional API cannot gate a stack that does not need it;
- `DependenciesAvailable` is still reported on its own and still `True`, so the optional API
  has not been folded into the required one;
- the module operator's ServiceAccount is granted no `watch` on `inferenceservices`, the
  verb that would make an absent CRD fatal again;
- both operators restart and stay healthy, which is the only way to see that they start with
  an empty informer set;
- a `TrustyAIService` reports `InferenceServicesPresent=False` / `ServingCRDNotFound` naming
  the CRD, and its operand still rolls out - payload capture being unavailable is not a
  degradation;
- deleting that service finishes: the finalizer reads the serving state too, and a read that
  failed there would leave the resource stuck on a cluster that has no such API.

```sh
k8s-tests test --section isvc-free                     # this section only
k8s-tests test --select '-m isvc_free'
```

The checks declare `release(min="3.7")`, so a run that declares 3.6 skips them and keeps
the CRD 3.6 needs.

### What `--section tas-payload` asserts

`tests/test_trustyai_serving_payload_capture.py` is the other side of the same API, and runs
*after* the section above: it **installs** the `InferenceService` stand-in, writes a mock
`InferenceService` beside a `TrustyAIService`, and takes the CRD back off the cluster when it
is done. The report and the run keep that order because the two cannot both be true at once.

The CRD comes from `$OPERATOR_REPO/tests/crds` - the file `deploy` applies for a run whose
operators watch the kind - so a run needs `OPERATOR_REPO` set, and a cluster that already
serves the kind keeps the CRD it has (only one the section installed is removed).

- the mock object gets `spec.predictor.logger = {mode: all, url:
  https://<service>.<namespace>.svc.cluster.local}`, the https sink a RawDeployment's
  predictor sidecar presents the service certificate to;
- the `TrustyAIService` reports `InferenceServicesPresent=True` / `InferenceServicesFound`,
  the mirror of the `ServingCRDNotFound` above;
- the module operator notices the API appearing without a restart and reports
  `KServeAvailable=True` / `Available` - it re-derives the state on every pass, which is why
  it registers no serving watch;
- a reconcile that finds the desired logger writes nothing: the object's `resourceVersion`
  is unchanged across two of them;
- deleting the service takes the logger back, along with the `internal.serving.kserve.io/*`
  annotations KServe writes beside it - the finalizer path `#1136` rewrote.

```sh
k8s-tests test --section tas-payload                   # this section only
k8s-tests test --select '-m tas_payload'
```

The object is created before the service on purpose: the operator registers no source for
`serving.kserve.io`, so the pass the service creation triggers is the one that finds it (a
fresh object is picked up within `defaultRequeueDelay`, 30s).

## Pytest suite

`tests/` holds the pytest checks that run against a live cluster; they are kept
separate from the harness so they can point at any cluster that runs the same
operators.

| Module | What it checks |
| --- | --- |
| `tests/test_trustyai_operators.py` | the harness smoke checks: both operator Deployments exist, ask for a single replica, and are healthy (`Available`, the desired replicas ready, their pods `Running` with every container ready) |
| `tests/test_trustyai_module.py` | the modular stack: the module CRD is installed, both operator Deployments are available, `TrustyAI/default-trustyai` reports `Ready=True` with `ProvisioningSucceeded=True` and an up-to-date `observedGeneration`, the module operator runs the built image, and - where the KServe CRD is installed - `KServeAvailable` reports `True`/`Available` (3.7 on), which is the other side of [the serving API being optional](#the-serving-api-is-optional) |
| `tests/test_trustyai_workload.py` | the workload operator image: it matches the `RELATED_IMAGE_*` value the module operator renders it from, the image the harness built, and the operator's own ConfigMap |
| `tests/test_emulator.py` | the LLM emulator (skipped when `MODEL_URL` is set): its Deployment runs the pinned image and is healthy, `/health` needs no token, `/v1/completions` and `/v1/chat/completions` answer `401` without the token or with another one and `200` with the token from the `vllm-emulator-auth` Secret, that Secret carries the `api-key` key `model.auth.secret_ref` resolves, and the token check reports which token fingerprint it saw |
| `tests/evalhub/test_evalhub_benchmark.py` | a benchmark job: the API accepts a job for `arc_easy` with `num_examples: 1` against the emulator or `MODEL_URL`, it reaches `completed`, its results carry the benchmark id, provider and numeric metrics, and - against the emulator - the emulator saw the tenant Secret's token (or `MODEL_KEY`) on a `/v1` request |
| `tests/evalhub/test_evalhub_adapters.py` | the EvalHub adapters section (opt-in, `k8s-tests test --section adapters`): each benchmark in its table - currently `ethics_cm` of `lm_evaluation_harness`, with `num_examples: 1` against the emulator or `MODEL_URL` - is accepted as the job's only benchmark, reaches `completed`, reports the benchmark, provider, numeric metrics and the `acc` its provider scores it on, turns its pass criteria into a test outcome, and - against the emulator - the emulator authenticated the model requests it made |
| `tests/evalhub/test_evalhub_atif.py` | the ATIF adapter section (opt-in, `k8s-tests test --section adapters-atif`): it deploys an MLflow tracking server and a stub judge, points the instance at MLflow, publishes a generated NeMo Gym export onto a claim, and asserts that the manifest drives discovery and its entry count, that the run's report keeps the manifest indices, digest verdict and the producer's `extra`, that a digest mismatch fails naming the file or withholds the score depending on `partial_result_policy`, that an unscored job publishes no `atif_overall_score` and reports `atif_complete: false`, that an empty manifest and an undeclared root field are invalid input, that ATIF-v1.6 still scores and ATIF-v1.9 fails naming the supported range |
| `tests/evalhub/test_evalhub_job_failure_reconciler.py` | the evaluation-job failure reconciler: an adapter image that cannot be pulled is reported by the operator instead of hanging - a malformed image reference fails within minutes naming `InvalidImageName` (3.6 on, part of the default run), and a nonexistent tag stays `pending` through the ten-minute grace [trustyai-service-operator#1139](https://github.com/trustyai-explainability/trustyai-service-operator/pull/1139) added and then fails with `ImagePullBackOff` in the benchmark's error message (3.7 on, own section `k8s-tests test --section image-pull-grace` because it waits out the grace, about 12 minutes); both leave the Kubernetes Job labelled `evaluation-phase=Failed` with `evalhub-failure-reported=true`, a Kind-only fixture teaches the operator's callback client the instance certificate, and `--keep-resources` keeps the job with its pod and adapter logs and its provider |
| `tests/evalhub/test_evalhub_collection_generation.py` | the authorization `trustyai-service-operator#1131` added for the collection generation API: the mounted policy maps `POST /api/v1/evaluations/collections/generation` to `evaluations create` and `GET`/`DELETE .../generation/{id}` to `evaluations` `{{.FromMethod}}`, each declared once with the `X-Tenant` rewrite and ahead of the generic `/api/v1/evaluations/collections/*` entry that also maps `get` and `delete`; the removed `.../generation/models` path has no mapping; the pod template's `auth.yaml` digest describes the policy the container mounts; the tenant-user Role grants `evaluations` `get`/`create`/`delete` and no collection write; and, as an identity granted exactly those rules, all three paths answer through the proxy (not `401`/`403`), need a token, and are refused when `X-Tenant` names a namespace the identity has no grant in |
| `tests/lmeval/test_lmeval_network_policy.py` | an `LMEvalJob`: the operator creates one NetworkPolicy, owned by the job, admitting no ingress and one allow-all egress rule, selecting the Pod on the identity labels it also stamps there; taking any of the four identity labels off the Pod is repaired (also for a completed job, where only the Pod watch is left to trigger it); editing the policy's rules is repaired while an annotation the operator does not own is kept; and deleting the policy brings a replacement with a new UID rather than reviving the old one |
| `tests/test_trustyai_serving_payload_capture.py` | payload capture on an installed API (`k8s-tests test --section tas-payload`, 3.7 on): installs the `InferenceService` stand-in, writes a mock `InferenceService` beside a `TrustyAIService`, and asserts the predictor's logger is that service's https sink in mode `all`, the `InferenceServicesPresent=True` / `InferenceServicesFound` condition, the module reporting the API `Available` without a restart, that a pass finding the desired logger writes nothing, and that deleting the service takes the logger and the `internal.serving.kserve.io/*` annotations back. Removes the CRD again. See [What `--section tas-payload` asserts](#what---section-tas-payload-asserts) |
| `tests/test_trustyai_serving_optional.py` | the install with no KServe API (`k8s-tests test --section isvc-free`, 3.7 on): no `inferenceservices.serving.kserve.io` on the cluster, the module reports `KServeAvailable=False` with reason `NotInstalled` and severity `Info` and never `PreConditionFailed`, `Ready=True` with the optional API reported separately from the required `DependenciesAvailable`, no `watch` on the kind granted to the module operator's ServiceAccount, both operators restart and stay healthy, and a `TrustyAIService` reports `InferenceServicesPresent=False` with reason `ServingCRDNotFound` while its operand still runs, and deleting it finishes. See [The serving API is optional](#the-serving-api-is-optional) |
| `tests/test_trustyai_service.py` | the TrustyAI service: the CRD serves `v1` and `v1alpha1` through a conversion webhook and a resource created as one reads back as the other; the operand rolls out one replica with the `trustyai-service`, `kube-rbac-proxy` and `internal-tls-proxy` containers, runs the requested image, carries the resource's schedule and storage settings, and mounts a bound claim at the requested size; `/q/health` answers `healthy` unauthenticated; `/info` needs a token and refuses one without the grant; both halves of a drift comparison upload and are counted; CompareMeans detects the shifted distribution; and a scheduled metric is listed once and deleted |
| `tests/evalhub/test_evalhub.py` | EvalHub: the CRD serves `v1` and `v1alpha1` through a conversion webhook; the instance the tests create reads back through both versions, reports `Ready`, runs one pod with the `evalhub` and `kube-rbac-proxy` containers and the labels the Service selects on; a namespace labelled as a tenant receives the discovery and CA ConfigMaps; and `/api/v1/health` answers `200` (and `405` to non-GET methods) |

Two axes select what runs, and they compose: a **directory** names a component
(`tests/evalhub`), a **marker** names a section within it (`evalhub`, `benchmark`,
`image_pull_grace`, `adapters`, `generation`, `isvc_free`, `tas_payload`, `module`,
`service`, `emulator`, `workload`).
Every module in a component directory carries that component's marker as well as its
own, so a section is both part of its component and runnable on its own.

```sh
k8s-tests test                                              # everything except the adapters and the image-pull grace case
k8s-tests test --section evalhub                            # all EvalHub checks, adapters and the grace case excluded
k8s-tests test --section generation                         # just the collection generation authorization
k8s-tests test --section image-pull-grace                   # just the ten-minute image-pull grace case (slow, own report block)
k8s-tests test --section adapters                           # just the adapter jobs (slow, own report)
k8s-tests test --section lmeval                             # just the LMEvalJob checks
k8s-tests test --section isvc-free                          # just the no-serving-API install
k8s-tests test --section tas-payload                        # the payload capture on an installed API
k8s-tests test --section all                                # everything except opt-in chaos, adapters included
```

Anything else goes through `--select`, which takes a marker expression, a directory,
a node id or `-k` - whatever pytest accepts:

```sh
k8s-tests test --select '-m generation'                    # one section
k8s-tests test --select tests/evalhub                       # one component directory
k8s-tests test --select tests/lmeval                        # the same, for another component
k8s-tests test --select '-m "evalhub and not adapters"'     # without its slow section
k8s-tests test --select '-m "evalhub and not benchmark and not adapters"'
k8s-tests test --select tests/evalhub/test_evalhub_collection_generation.py
k8s-tests test --select tests/evalhub/test_evalhub.py::test_evalhub_health_endpoint
k8s-tests test --select '-k tenant_role'                    # one check, by name
```

For example: `-m module` the modular stack, `-m workload` the workload operator,
`-m lmeval` an `LMEvalJob` and its NetworkPolicy, `-m service` the TrustyAI service,
`-m emulator` the LLM emulator, `-m benchmark` the harness smoke job, `-m isvc_free`
the install with no serving API, `-m tas_payload` the payload capture written on top of
one. The same
selections run through `k8s-tests test --pytest-args ...` when a full run with an extra
filter is what you want; `test-select` runs only the selection.

`-m evalhub` includes the adapter jobs, because they are EvalHub checks too; they
are excluded from `k8s-tests test` and `--section evalhub` by the `adapters` marker
rather than by not being marked, and the image-pull-grace case is excluded the same
way by the `image_pull_grace` marker: it waits out the operator's ten-minute
image-pull grace. The `invalid-image-name` case needs no grace and stays in the
default run. See [EvalHub adapters](#evalhub-adapters).

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
k8s-tests test --pytest-args "-v"                          # extra pytest flags
k8s-tests test --kube-context my-cluster --namespace opendatahub   # another cluster
```

### Operator chaos tests

Run these only on a disposable test cluster with TrustyAI already deployed. The test
intentionally interrupts the single-replica operator it targets. Run it serially, not
alongside other cluster tests.

```sh
uv sync
uv run k8s-tests test --chaos --kube-context kind-trustyai --namespace opendatahub
# Or, with the context and namespace set in the environment:
uv run pytest --chaos
```

If a previously installed `k8s-tests` command cannot import Hypothesis, use
`uv run k8s-tests` from this checkout as shown above, or refresh the standalone
installation with `uv tool install --force .` from the repository root.

`--chaos` runs only `tests/chaos/`. Without the flag, those checks are deselected even
with `--section all` or an explicit path. Use `--chaos -k workload-operator` with pytest
(or `--chaos --select '-k workload-operator'` with the harness) to target one operator.

Hypothesis generates short sequences of one to three Pod deletions for each operator:
the workload operator and, on modular installs, the module operator. The Kubernetes
client verifies Pod ownership through the ReplicaSet, deletes exactly that Pod by UID,
and waits for the same Deployment to recover with a new, Ready Pod before the next
deletion. These Pod-only checks leave Deployments intact.

Two additional cases are inspired by [Sieve's intermediate-state testing pattern](https://github.com/sieve-project/sieve/blob/763cd38/README.md#testing-approaches).
On an existing **Managed** modular install, they delete the workload operator Deployment
by UID, first with the module operator running and then immediately after requesting
deletion of its Pod. Both require the module operator to recreate the workload
Deployment with a new UID, the same images and replica count, and healthy Pods.
The overlapping-fault case uses Kubernetes APIs, not Sieve instrumentation: it does
not guarantee interruption at a particular point in reconciliation or that an event
was missed. Run just these cases with `--chaos --select tests/chaos/test_module_reconciliation.py`.

Chaos does not directly modify user workloads or custom resources. The test identity
needs read access to Deployments and ReplicaSets and permission to list and delete Pods
in the operator namespace. The additional scenarios also require read access to the
TrustyAI module API and permission to delete the workload operator Deployment.

### Keeping the resources

By default the fixtures remove what they created. Set `KEEP_RESOURCES=1` - or run
`k8s-tests test --keep-resources` - to leave it
all in place and look at it afterwards:

```sh
k8s-tests test --keep-resources
kubectl -n "$(kubectl get ns -o name | grep trustyai-evalhub | head -1 | cut -d/ -f2)" get all,cm,evalhubs,jobs
kubectl -n system get deploy,svc,cm,secret -l app=vllm-emulator
```

That keeps the EvalHub instance with its tenant namespace, the LLM emulator with
its Secret and wrapper ConfigMap, and the evaluation job with its spec ConfigMap,
its pod and the adapter logs. The run logs each namespace it leaves behind and the
HTML report says so too. Nothing is kept silently: with the default setting those
are removed at the end of the session.

The two operators - and the module resource `k8s-tests up` applied - are not the
fixtures' to remove: they stay until `k8s-tests down` deletes the cluster, or until you
delete the resource, which takes the workload operator and its operands with it.
A session that had to create the resource itself, because no `k8s-tests deploy` had run
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
captured log and failure detail per test: `reports/report.html` for `k8s-tests test`
and `--section all`, `reports/adapters.html` for `--section adapters`.
The run is ordered by scope and the report shows it: one block each for the EvalHub
adapters, EvalHub, the LLM emulator, an `LMEvalJob`, the module operator, the
TrustyAI service and the workload operator, a `Scope` column naming the block its
first row starts, and a per-scope count above the table.

```sh
k8s-tests test --report /tmp/trustyai.html                   # choose the report path
k8s-tests test --section adapters --report /tmp/a.html       # the same for the adapters
```

Checks run against `KUBE_CONTEXT` (default `kind-trustyai`) and `NAMESPACE`
(default `system`), the values the harness uses by default. A missing kubeconfig
or context skips the suite; a missing Deployment fails with a pointer to
`k8s-tests up`.

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
  under it, `utilities/stack.py` runs those steps for `k8s-tests deploy`,
  `utilities/trustyai_service.py` owns the whole `TrustyAIService` workload
  lifecycle, `utilities/portforward.py` hands out local ports for the forwards,
  `utilities/model.py` chooses the model endpoint (`MODEL_URL` and `MODEL_KEY`)
  and writes the credential Secret it resolves, and the other modules hold the
  per-component helpers
- `conftest.py` — session fixtures for the context, namespace and API clients
- `harness/` — the CLI: settings, cluster, images, registry, deploy, selections
- `pyproject.toml` — the dependencies (`click`, `kubernetes`, `pytest`, `requests`,
  `cryptography`, `pyyaml`) with the `pytest-html` and `ruff` dev group, the marker
  registry, the console script and the `ruff` configuration; `uv.lock` pins them and
  `uv run` runs them

`k8s-tests lint` runs `ruff check` and `ruff format --check`; `k8s-tests fmt` fixes both.
`.github/workflows/python-lint.yaml` runs `k8s-tests lint` in CI on pushes to
`main` and on pull requests.

## GitHub Actions

`.github/workflows/kind-harness.yaml` runs the harness end to end on a GitHub runner: it
clones `trustyai-explainability/trustyai-service-operator` - `main`, or a branch, tag or
commit named when the run is started - builds the operator images with the runner's
Docker, creates the Kind cluster from `hack/kind/cluster.yaml`, deploys the operators
from that checkout and runs `k8s-tests test` against them.

The workflow is called **Tier 3: AI Safety end-to-end**. It is manual only, **Actions** ->
**Tier 3: AI Safety end-to-end** -> **Run workflow**, and that is the permission gate:
GitHub lets the repository's writers start a manual workflow and nobody else, so there is
no push, pull-request or schedule path an outside account could take. The job also refuses
to run anywhere but `trustyai-explainability/k8s-tests`, so a fork that copies the file gets
a no-op instead of a run of its own checkout.

The run page carries the results, not just a pointer to them: the workflow asks the checks
for a JUnit report (`--pytest-args "--junitxml=reports/junit.xml"`) and appends
`k8s-tests summary reports/junit.xml` to the job summary, which renders the same list of
checks `report.html` holds as a markdown table - one row per check with pass, fail, error or
skip, the totals, and the message of everything that failed or skipped. The same command
renders a report you already have:

```sh
k8s-tests summary reports/junit.xml                  # the run's checks as markdown
k8s-tests summary --help
```

Every run also uploads the `kind-harness-report` artifact, which holds the self-contained
`report.html` (download it and open it in a browser), `junit.xml` and, when the run failed,
`diagnostics.txt` with the pods, the recent events and the `TrustyAI` resource as the
cluster had them at the end. What runs is the default section, so the adapter benchmarks
- one submitted job each - are not part of it.

The input takes a branch, tag or commit of the operator repository; an unmerged pull
request ref (`refs/pull/...`) is refused, so a run cannot be pointed at a fork's
contribution. The `kind` and `kubectl` binaries are checked against the digests pinned in
the workflow before they are installed, because a release version does not pin what it
downloads.
