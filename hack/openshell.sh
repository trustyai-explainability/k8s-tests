#!/usr/bin/env bash
# Install the OpenShell gateway - and the Agent Sandbox controller it runs its
# sandboxes on - into the Kind cluster. `make openshell` runs this, and
# `make up OPENSHELL=1` runs it after the TrustyAI operators.
#
# The gateway is an option because Kind's default CNI (kindnet) accepts the
# NetworkPolicies OpenShell creates but does not enforce them: this gets the
# gateway and its sandboxes running, not the isolation the gateway exists for.
# Read the README before recording an isolation result from a Kind cluster.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

helm_bin="${HELM:-helm}"
namespace="${OPENSHELL_NAMESPACE:-openshell}"
release="${OPENSHELL_RELEASE:-openshell}"
chart="${OPENSHELL_CHART:-oci://ghcr.io/nvidia/openshell/helm-chart}"
chart_version="${OPENSHELL_CHART_VERSION:-0.1.4}"
default_agent_sandbox_manifest="https://github.com/kubernetes-sigs/agent-sandbox/releases/latest/download/sandbox.yaml"
agent_sandbox_manifest="${AGENT_SANDBOX_MANIFEST:-$default_agent_sandbox_manifest}"
wait_timeout="${TIMEOUT:-300s}"
allow_unauthenticated="${OPENSHELL_ALLOW_UNAUTHENTICATED:-true}"
dry_run=false
uninstall=false

usage() {
	cat <<'EOF'
usage: hack/openshell.sh [--dry-run | --uninstall]

Installs the Agent Sandbox controller and the pinned OpenShell gateway into the
Kind cluster named by KIND_CLUSTER (context KUBE_CONTEXT). Set the variables
the Makefile exports to change what it installs; see the README.

  --dry-run     print the commands instead of running them
  --uninstall   remove the Helm release (leaves Agent Sandbox and the namespace)
EOF
}

while (($#)); do
	case $1 in
	--dry-run) dry_run=true ;;
	--uninstall) uninstall=true ;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		usage >&2
		die "unknown argument: $1"
		;;
	esac
	shift
done

# run prints a command in --dry-run mode and executes it otherwise, so the
# ordering of the install can be reviewed without a cluster or Helm.
run() {
	if "$dry_run"; then
		printf '  %s\n' "$*"
		return
	fi
	"$@"
}

require_server_version() {
	local version major minor
	version=$(kube version -o json 2>/dev/null | sed -n 's/.*"gitVersion": *"\([^"]*\)".*/\1/p' || true)
	[[ -n "$version" ]] || die "cannot read the server version for context ${KUBE_CONTEXT:-kind-kind}"
	if [[ $version =~ ^v([0-9]+)\.([0-9]+) ]]; then
		major=${BASH_REMATCH[1]}
		minor=${BASH_REMATCH[2]}
		if ((major < 1 || (major == 1 && minor < 29))); then
			die "OpenShell needs Kubernetes 1.29 or newer; ${KUBE_CONTEXT:-kind-kind} is $version" \
				"(pin KIND_NODE_IMAGE to a newer node image)"
		fi
	else
		warn "could not parse the server version '$version'; OpenShell needs Kubernetes 1.29 or newer"
	fi
}

# require_policy_cni reports whether the cluster can enforce the NetworkPolicies
# the gateway creates. Kind's default kindnet cannot.
require_policy_cni() {
	if ! kube -n kube-system get pods -o name 2>/dev/null | grep -Eq 'pod/(calico-node|cilium)'; then
		warn "no policy-enforcing CNI (Calico or Cilium) found: Kind's kindnet accepts"
		warn "NetworkPolicy objects but does not enforce them, so sandbox egress isolation"
		warn "is not real on this cluster; do not record isolation results from it"
	fi
}

require_default_storage_class() {
	if ! kube get storageclass -o name 2>/dev/null | grep -q .; then
		warn "the cluster has no StorageClass: the gateway's SQLite volume and the"
		warn "sandbox workspace volumes will stay Pending; install a default StorageClass"
		return
	fi
	if ! kube get storageclass 2>/dev/null | grep -q '(default)'; then
		warn "no StorageClass is marked default: set server.dataStorageClass and"
		warn "server.workspaceStorageClass, or make one StorageClass the default"
	fi
}

uninstall_gateway() {
	if "$dry_run"; then
		printf '  %s\n' "$helm_bin uninstall $release --namespace $namespace"
		return
	fi
	if "$helm_bin" status "$release" --namespace "$namespace" >/dev/null 2>&1; then
		info "uninstalling the OpenShell release $release from namespace $namespace"
		"$helm_bin" uninstall "$release" --namespace "$namespace" --wait --timeout "$wait_timeout"
	else
		info "OpenShell release $release is not installed in namespace $namespace"
	fi
	warn "namespace $namespace and its PVCs are left in place; delete the namespace to remove them"
	warn "the Agent Sandbox controller and CRDs are cluster-scoped and left in place"
}

if "$uninstall"; then
	if ! "$dry_run"; then
		command -v "$helm_bin" >/dev/null 2>&1 || die "helm 3 is required to uninstall the OpenShell chart; install it or set HELM"
		command -v "${KUBECTL:-kubectl}" >/dev/null 2>&1 || die "kubectl is required"
	fi
	uninstall_gateway
	exit 0
fi

if ! "$dry_run"; then
	command -v "$helm_bin" >/dev/null 2>&1 || die "helm 3 is required to install the OpenShell chart; install it or set HELM"
	command -v "${KUBECTL:-kubectl}" >/dev/null 2>&1 || die "kubectl is required"
	require_server_version
	require_default_storage_class
	require_policy_cni
fi

info "installing the Agent Sandbox controller and CRDs from $agent_sandbox_manifest"
run kube apply -f "$agent_sandbox_manifest"
run kube -n agent-sandbox-system rollout status deployment/agent-sandbox-controller \
	--timeout "$wait_timeout"

info "creating namespace $namespace"
if "$dry_run"; then
	printf '  %s\n' "kube create namespace $namespace --dry-run=client -o yaml | kube apply -f -"
else
	kube create namespace "$namespace" --dry-run=client -o yaml | kube apply -f -
fi

# workspaceMode=shared with one sandbox namespace is the model the sandbox work
# targets; the gateway StatefulSet keeps its SQLite database on a PVC, and each
# sandbox gets a workspace PVC, both on the default StorageClass.
helm_args=(
	upgrade --install "$release" "$chart"
	--version "$chart_version"
	--namespace "$namespace"
	--wait
	--timeout "$wait_timeout"
	--set "server.drivers.kubernetes.workspaceMode=shared"
	--set "server.sandboxNamespace=$namespace"
	--set "workload.kind=statefulset"
	--set "fullnameOverride=$release"
)
if [[ $allow_unauthenticated == true ]]; then
	# A local Kind cluster has no OIDC provider or user directory, so the
	# gateway would otherwise reject every client. Turn this off to exercise
	# the gateway's authentication.
	helm_args+=(--set "server.auth.allowUnauthenticatedUsers=true")
fi

info "installing the OpenShell gateway ($chart:$chart_version) in namespace $namespace"
run "$helm_bin" "${helm_args[@]}"

run kube -n "$namespace" wait --for=condition=Ready pod \
	-l "app.kubernetes.io/name=openshell,app.kubernetes.io/instance=$release" \
	--timeout "$wait_timeout"

info "OpenShell gateway $release is running in namespace $namespace"
info "  port-forward it with:"
info "  ${KUBECTL:-kubectl} --context ${KUBE_CONTEXT:-kind-kind} -n $namespace port-forward svc/$release 8080:8080"
if [[ $allow_unauthenticated == true ]]; then
	info "  it accepts unauthenticated clients (OPENSHELL_ALLOW_UNAUTHENTICATED=true);"
	info "  set that to false to exercise the gateway's authentication"
fi

