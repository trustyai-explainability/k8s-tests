"""Sieve-inspired recovery checks using only public Kubernetes APIs."""

import pytest
from kubernetes import client
from kubernetes.dynamic import DynamicClient

from utilities.operator_chaos import check_workload_deployment_recovery
from utilities.release import release
from utilities.trustyai import (
    MODULE_MANAGEMENT_STATE,
    module_api_version,
    module_exists,
    read_module,
)

pytestmark = [pytest.mark.chaos, pytest.mark.module, release(min="3.6")]


@pytest.mark.usefixtures("modular_trustyai_architecture")
@pytest.mark.parametrize(
    "restart", [pytest.param(False, id="module-running"), pytest.param(True, id="module-restarting")]
)
def test_module_recreates_workload_operator(
    apps_client: client.AppsV1Api,
    core_client: client.CoreV1Api,
    dynamic_client: DynamicClient,
    namespace: str,
    restart: bool,
) -> None:
    api_version = module_api_version(apps_client.api_client)
    if not module_exists(dynamic_client, api_version):
        pytest.skip("requires an existing TrustyAI module; deploy the modular stack before running chaos")
    module = read_module(dynamic_client, api_version)
    if module["spec"].get("managementState") != MODULE_MANAGEMENT_STATE:
        pytest.skip("requires a Managed TrustyAI module")
    check_workload_deployment_recovery(apps_client, core_client, namespace, module["metadata"]["uid"], restart=restart)
