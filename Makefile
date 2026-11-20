# Local Kind harness for the TrustyAI module and workload operators.
#
#   make up OPERATOR_REPO=/path/to/trustyai-service-operator
#   make up OPERATOR_REPO=/path/to/trustyai-service-operator OPENSHELL=1
#
# creates a Kind cluster, builds the workload and module operator images from
# that checkout, loads them into the cluster, and deploys the module operator
# pointed at the freshly built workload operator image. The module operator then
# deploys the workload operator, which deploys EvalHub. The LLM emulator the
# adapter checks call as their model is deployed and removed by the tests.
# OPENSHELL=1 also installs the OpenShell gateway and the Agent Sandbox
# controller it needs; see the README for what that can and cannot prove on Kind.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -o pipefail -ec
.DEFAULT_GOAL := help

KUBECTL ?= kubectl
KIND ?= kind
HELM ?= helm

KIND_CLUSTER ?= trustyai
KIND_CONFIG ?= hack/kind/cluster.yaml
# Empty uses the node image Kind ships with.
KIND_NODE_IMAGE ?=
KUBE_CONTEXT ?= kind-$(KIND_CLUSTER)
# Leave empty to let Kind pick: docker when it is usable, otherwise podman.
# Set to `podman` or `docker` to force a provider on machines that have both.
KIND_PROVIDER ?=
KIND_ENV := $(if $(KIND_PROVIDER),KIND_EXPERIMENTAL_PROVIDER=$(KIND_PROVIDER))

# The optional OpenShell gateway. `make up OPENSHELL=1` installs it after the
# operators; `make openshell` installs it on a cluster that is already up.
# Kind's default CNI accepts the gateway's NetworkPolicies but does not enforce
# them, so this exercises the gateway's API and sandbox lifecycle, not the
# isolation it provides; see the README before recording isolation results.
OPENSHELL ?=
OPENSHELL_NAMESPACE ?= openshell
OPENSHELL_RELEASE ?= openshell
OPENSHELL_CHART ?= oci://ghcr.io/nvidia/openshell/helm-chart
# OpenShell 0.1.4, the version the sandbox spike targets. Override deliberately.
OPENSHELL_CHART_VERSION ?= 0.1.4
# Local Kind has no OIDC provider, so the gateway would reject every client.
OPENSHELL_ALLOW_UNAUTHENTICATED ?= true
AGENT_SANDBOX_MANIFEST ?= https://github.com/kubernetes-sigs/agent-sandbox/releases/latest/download/sandbox.yaml

NAMESPACE ?= system

# Checkout of trustyai-service-operator used for the images and the manifests.
# Required by `up`, `images` and `deploy`; there is no default.
OPERATOR_REPO ?=
TAG ?= dev
IMAGE_PREFIX ?= localhost
WORKLOAD_IMG ?= $(IMAGE_PREFIX)/trustyai-service-operator:$(TAG)
MODULE_IMG ?= $(IMAGE_PREFIX)/trustyai-operator-module:$(TAG)
# Leave empty to use docker when available, otherwise podman.
ENGINE ?=

# The LLM emulator the checks call as their model is deployed and removed by the
# pytest fixtures (see utilities/emulator.py), not by this harness. These are the
# settings those fixtures read; leave them empty to use their defaults.
EMULATOR_IMG ?=
EMULATOR_NAMESPACE ?=
EMULATOR_TOKEN ?=

# A real model endpoint the job checks call instead of the emulator. MODEL_URL is
# an OpenAI-compatible base URL (for example http://model:8000/v1); when it is set
# the fixtures deploy no emulator. MODEL_KEY, when set, becomes the `api-key`
# entry of the `model-credentials` Secret the fixtures create in the tenant
# namespace, which the job names in model.auth.secret_ref. MODEL_NAME overrides
# the model name a job submits.
MODEL_URL ?=
MODEL_KEY ?=
MODEL_NAME ?=

# Set to 1 to leave everything the run created on the cluster (namespaces, the
# LLM emulator, the evaluation job) instead of removing it at the end.
KEEP_RESOURCES ?=
# EvalHub operand image passed to the workload operator through the module
# operator's RELATED_IMAGE_ODH_EVAL_HUB_IMAGE. Empty keeps the operator's own
# pin (quay.io/evalhub/evalhub:latest in its params.env).
EVALHUB_IMG ?=

# TrustyAI service operand image passed through
# RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_PY_IMAGE. The operator's ODH overlay pins
# quay.io/opendatahub/trustyai-service:latest, which is the retired Java service:
# it mounts a serving key the current deployment template no longer provides and
# crash-loops on /etc/tls/internal/tls.crt. The default here is the Python
# rewrite the operator is written for; set it to a build under test to override.
TRUSTYAI_SERVICE_IMG ?= quay.io/trustyai/trustyai-service:latest
TIMEOUT ?= 300s
PYTEST_ARGS ?=
REPORT_FILE ?= reports/report.html

# The EvalHub adapter checks are a section of their own: each one submits a real
# benchmark, which fetches its dataset from the Hub and takes minutes, so
# `make test` leaves the marker out and `make test-evalhub-adapters` runs nothing
# else.
ADAPTERS_MARKER ?= adapters
ADAPTERS_REPORT_FILE ?= reports/adapters.html

export KIND KUBECTL KIND_CLUSTER KUBE_CONTEXT NAMESPACE OPERATOR_REPO TAG \
	WORKLOAD_IMG MODULE_IMG EVALHUB_IMG TRUSTYAI_SERVICE_IMG ENGINE TIMEOUT \
	REPORT_FILE ADAPTERS_MARKER ADAPTERS_REPORT_FILE EMULATOR_IMG EMULATOR_NAMESPACE EMULATOR_TOKEN \
	MODEL_URL MODEL_KEY MODEL_NAME KEEP_RESOURCES HELM OPENSHELL OPENSHELL_NAMESPACE \
	OPENSHELL_RELEASE OPENSHELL_CHART OPENSHELL_CHART_VERSION OPENSHELL_ALLOW_UNAUTHENTICATED \
	AGENT_SANDBOX_MANIFEST

.PHONY: help
help: ## Display this help.
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_0-9-]+:.*## / {names[++n] = $$1; desc[n] = $$2; if (length($$1) > width) width = length($$1)} END {for (i = 1; i <= n; i++) printf "  \033[36m%-*s\033[0m %s\n", width, names[i], desc[i]}' $(MAKEFILE_LIST)

.PHONY: cluster
cluster: ## Create the Kind cluster (idempotent).
	$(KIND_ENV) $(KIND) get clusters | grep -qx '$(KIND_CLUSTER)' || \
		$(KIND_ENV) $(KIND) create cluster --name $(KIND_CLUSTER) --config $(KIND_CONFIG) \
			$(if $(KIND_NODE_IMAGE),--image $(KIND_NODE_IMAGE)) --wait 5m
	$(KUBECTL) --context $(KUBE_CONTEXT) wait --for=condition=Ready nodes --all --timeout $(TIMEOUT)

.PHONY: cluster-delete
cluster-delete: ## Delete the Kind cluster.
	$(KIND_ENV) $(KIND) delete cluster --name $(KIND_CLUSTER)

.PHONY: check-operator-repo
check-operator-repo:
	@test -n "$(OPERATOR_REPO)" || { \
		echo "error: OPERATOR_REPO is required, e.g. make $(MAKECMDGOALS) OPERATOR_REPO=/path/to/trustyai-service-operator" >&2; \
		exit 2; \
	}
	@test -d "$(OPERATOR_REPO)" || { echo "error: OPERATOR_REPO=$(OPERATOR_REPO) does not exist" >&2; exit 2; }
	@test -f "$(OPERATOR_REPO)/Dockerfile" -a -f "$(OPERATOR_REPO)/trustyai-operator-module/Dockerfile" || { \
		echo "error: $(OPERATOR_REPO) does not look like a trustyai-service-operator checkout" >&2; \
		exit 2; \
	}

.PHONY: images
images: check-operator-repo ## Build the workload and module operator images from OPERATOR_REPO.
	hack/build-images.sh

.PHONY: load
load: ## Load the built images into the Kind cluster.
	hack/load-images.sh

.PHONY: deploy
deploy: check-operator-repo ## Deploy the module operator, create its module resource, and wait for the workload operator.
	hack/deploy.sh

.PHONY: openshell
openshell: cluster ## Install the OpenShell gateway and the Agent Sandbox controller on the Kind cluster.
	hack/openshell.sh

.PHONY: openshell-status
openshell-status: ## Show the OpenShell gateway, its Agent Sandbox controller, and their pods.
	$(KUBECTL) --context $(KUBE_CONTEXT) get deployment,pods -A -l app=agent-sandbox-controller --ignore-not-found
	$(KUBECTL) --context $(KUBE_CONTEXT) get statefulset,service,pods -A \
		-l app.kubernetes.io/name=openshell --ignore-not-found

.PHONY: openshell-uninstall
openshell-uninstall: ## Remove the OpenShell gateway release (leaves Agent Sandbox and the namespace).
	hack/openshell.sh --uninstall

OPENSHELL_TARGET = $(if $(filter 1,$(OPENSHELL)),openshell)

.PHONY: up
up: check-operator-repo cluster images load deploy $(OPENSHELL_TARGET) ## Create the cluster, build, load, deploy, and wait for both operators; OPENSHELL=1 also installs the gateway.

.PHONY: down
down: cluster-delete ## Delete the Kind cluster.

.PHONY: clean
clean: ## Delete the built operator images and the HTML report (leaves the cluster).
	hack/clean.sh

.PHONY: test
test: ## Run every section except the EvalHub adapters, and write an HTML report.
	@uv run pytest -m 'not $(ADAPTERS_MARKER)' $(PYTEST_ARGS) --html=$(REPORT_FILE) --self-contained-html || status=$$?; \
		echo "HTML report: $(abspath $(REPORT_FILE))"; \
		exit $${status:-0}

.PHONY: test-evalhub-adapters
test-evalhub-adapters: ## Run only the EvalHub adapter checks, and write their own HTML report.
	@uv run pytest -m '$(ADAPTERS_MARKER)' $(PYTEST_ARGS) --html=$(ADAPTERS_REPORT_FILE) --self-contained-html || status=$$?; \
		echo "HTML report: $(abspath $(ADAPTERS_REPORT_FILE))"; \
		exit $${status:-0}

# Selection has two axes: a directory names a component (tests/evalhub), a marker
# names a section within it (evalhub, benchmark, adapters, generation, ...).
# The targets below name the common combinations; test-select takes any pytest
# selection, so a section, a directory, a node id or -k all work:
#
#   make test-select SELECT='-m generation'
#   make test-select SELECT='-m "evalhub and not adapters"'
#   make test-select SELECT=tests/evalhub
#   make test-select SELECT=tests/evalhub/test_evalhub.py::test_evalhub_health_endpoint
#   make test-select SELECT='-k tenant_role'

.PHONY: test-evalhub
test-evalhub: ## Run every EvalHub check except the adapter section.
	@uv run pytest -m 'evalhub and not $(ADAPTERS_MARKER)' $(PYTEST_ARGS) --html=$(REPORT_FILE) --self-contained-html || status=$$?; \
		echo "HTML report: $(abspath $(REPORT_FILE))"; \
		exit $${status:-0}

.PHONY: test-evalhub-generation
test-evalhub-generation: ## Run only the collection generation authorization checks.
	@uv run pytest -m 'evalhub and generation' $(PYTEST_ARGS) --html=$(REPORT_FILE) --self-contained-html || status=$$?; \
		echo "HTML report: $(abspath $(REPORT_FILE))"; \
		exit $${status:-0}

.PHONY: test-lmeval
test-lmeval: ## Run the LMEvalJob checks, the Pod and the NetworkPolicy that guards it.
	@uv run pytest -m 'lmeval' $(PYTEST_ARGS) --html=$(REPORT_FILE) --self-contained-html || status=$$?; \
		echo "HTML report: $(abspath $(REPORT_FILE))"; \
		exit $${status:-0}

.PHONY: test-select
test-select: ## Run one selection, e.g. SELECT='-m generation' or SELECT=tests/evalhub.
	@test -n '$(strip $(SELECT))' || { \
		echo "error: SELECT is required, e.g. make test-select SELECT='-m generation' or SELECT=tests/evalhub" >&2; \
		exit 2; \
	}
	@uv run pytest $(SELECT) $(PYTEST_ARGS) --html=$(REPORT_FILE) --self-contained-html || status=$$?; \
		echo "HTML report: $(abspath $(REPORT_FILE))"; \
		exit $${status:-0}

.PHONY: test-all
test-all: ## Run every section, the EvalHub adapters included.
	@uv run pytest $(PYTEST_ARGS) --html=$(REPORT_FILE) --self-contained-html || status=$$?; \
		echo "HTML report: $(abspath $(REPORT_FILE))"; \
		exit $${status:-0}

.PHONY: test-keep
test-keep: ## Run the suite and leave every resource on the cluster for inspection.
	$(MAKE) test KEEP_RESOURCES=1

.PHONY: lint
lint: ## Lint and format-check the Python sources.
	uv run ruff check .
	uv run ruff format --check .

.PHONY: fmt
fmt: ## Format and autofix the Python sources.
	uv run ruff format .
	uv run ruff check --fix .

.PHONY: status
status: ## Show the module CR, the operators, EvalHub, the emulator (if a run left one), and their pods.
	$(KUBECTL) --context $(KUBE_CONTEXT) get trustyais.components.platform.opendatahub.io --ignore-not-found
	$(KUBECTL) --context $(KUBE_CONTEXT) -n $(NAMESPACE) get deployment,pods \
		-l app.kubernetes.io/name=trustyai-operator-module --ignore-not-found
	$(KUBECTL) --context $(KUBE_CONTEXT) -n $(NAMESPACE) get deployment,pods \
		-l control-plane=trustyai-service-operator --ignore-not-found
	$(KUBECTL) --context $(KUBE_CONTEXT) get evalhubs.trustyai.opendatahub.io -A --ignore-not-found
	$(KUBECTL) --context $(KUBE_CONTEXT) get deploy,pods -A \
		-l app=eval-hub --ignore-not-found
	$(KUBECTL) --context $(KUBE_CONTEXT) -n $(if $(EMULATOR_NAMESPACE),$(EMULATOR_NAMESPACE),$(NAMESPACE)) \
		get deployment,service,pods -l app=vllm-emulator --ignore-not-found

.PHONY: logs
logs: ## Tail the module and workload operator logs.
	$(KUBECTL) --context $(KUBE_CONTEXT) -n $(NAMESPACE) logs \
		-l control-plane=controller-manager --tail=100 -f
