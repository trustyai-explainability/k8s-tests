"""Checks for the collection generation API and the authorization that fronts it.

trustyai-service-operator#1131 authorized the generation endpoints in the
kube-rbac-proxy policy: `POST /api/v1/evaluations/collections/generation` and
`GET`/`DELETE /api/v1/evaluations/collections/generation/{id}`. The policy maps
them to the `evaluations` pseudo-resource rather than `collections`, so a tenant
that may submit an evaluation may generate a collection without being able to
write one, and it declares them ahead of the generic
`/api/v1/evaluations/collections/*` entry that would otherwise match first.

Three independent things can regress, so they are checked separately: the policy
in the ConfigMap the instance mounts, the mount itself (the pod template's digest
annotation has to describe the policy the pod reads), and the tenant Role the
operator writes, which has to keep the generation lifecycle inside `evaluations`.

The API checks then call the paths through the port-forward as an identity granted
exactly the operator's tenant-user rules, whose only `collections` verbs are `get`,
`list` and `watch`. That is what makes them discriminating rather than a mere
reachability probe: a `DELETE` or a `POST` that fell through to the generic
collections entry would ask for a `collections` verb the identity does not have,
and the proxy would answer 403.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from kubernetes import client

from utilities.evalhub import (
    EVALHUB_API_GROUP,
    EVALHUB_NAME,
    TENANT_USER_IDENTITY_NAME,
    TENANT_USER_RULES,
    api_headers,
    create_tenant_identity,
    request,
)
from utilities.evalhub_auth import (
    AUTH_CONFIG_HASH_ANNOTATION,
    AUTH_CONFIG_KEY,
    COLLECTIONS_WILDCARD_PATH,
    GENERATION_MODELS_PATH,
    GENERATION_PATH,
    GENERATION_WILDCARD_PATH,
    METHOD_VERB_REWRITE,
    TENANT_HEADER,
    TENANT_NAMESPACE_REWRITE,
    auth_config_digest,
    auth_config_map,
    auth_policy,
    endpoint_index,
    endpoint_paths,
    mapping_resources,
    resource_attributes,
    rule_verbs,
    single_mapping,
    tenant_user_role,
)
from utilities.kube import mint_token, read_deployment
from utilities.release import release

LOGGER = logging.getLogger(__name__)

# The section's premise is 3.6: the operator names the policy ConfigMap after the
# instance and a digest of its policy, and the generation paths exist to be authorized.
pytestmark = [pytest.mark.evalhub, pytest.mark.generation, release(min="3.6")]

# The methods the policy has to split across the two generation entries, and the
# verb each one maps to.
GENERATION_CREATE_METHODS = ["post"]
GENERATION_LIFECYCLE_METHODS = ["get", "delete"]
# The verbs a tenant-user grant needs for the generation lifecycle, and the
# collection writes it must not have.
GENERATION_VERBS = {"get", "create", "delete"}
COLLECTION_WRITE_VERBS = {"create", "update", "patch", "delete"}


@pytest.fixture(scope="module")
def policy_config_map(
    core_client: client.CoreV1Api,
    apps_client: client.AppsV1Api,
    evalhub_instance: dict[str, Any],
    evalhub_instance_namespace: str,
) -> client.V1ConfigMap:
    """The ConfigMap the running proxy container reads its policy from."""
    deployment = read_deployment(apps_client, name=EVALHUB_NAME, namespace=evalhub_instance_namespace)
    config_map = auth_config_map(core_client, deployment, evalhub_instance_namespace)
    assert config_map.metadata.name.startswith(f"{EVALHUB_NAME}-config-"), (
        f"the policy ConfigMap is named {config_map.metadata.name}; the operator names it after the instance and a "
        "digest of its policy, so that a new policy is a new ConfigMap"
    )
    return config_map


@pytest.fixture(scope="module")
def policy(policy_config_map: client.V1ConfigMap) -> dict[str, Any]:
    """The policy the proxy enforces, parsed from the ConfigMap it mounts."""
    parsed = auth_policy(policy_config_map)
    LOGGER.info("declared authorization endpoints: %s", endpoint_paths(parsed))
    return parsed


@pytest.fixture(scope="module")
def tenant_role(
    rbac_client: client.RbacAuthorizationV1Api,
    evalhub_instance: dict[str, Any],
    evalhub_tenant_namespace: str,
) -> client.V1Role:
    """The Role the operator wrote for the tenant namespace's own ServiceAccounts."""
    return tenant_user_role(rbac_client, evalhub_tenant_namespace)


@pytest.fixture(scope="module")
def tenant_user_headers(
    core_client: client.CoreV1Api,
    api_client: client.ApiClient,
    kube_context: str,
    evalhub_instance: dict[str, Any],
    evalhub_tenant_namespace: str,
) -> dict[str, str]:
    """A bearer token for an identity granted exactly the operator's tenant-user rules."""
    create_tenant_identity(
        core_client,
        client.RbacAuthorizationV1Api(api_client),
        evalhub_tenant_namespace,
        name=TENANT_USER_IDENTITY_NAME,
        rules=TENANT_USER_RULES,
    )
    token = mint_token(kube_context, evalhub_tenant_namespace, TENANT_USER_IDENTITY_NAME)
    return api_headers(token, evalhub_tenant_namespace)


def resource_of(policy: dict[str, Any], path: str) -> dict[str, Any]:
    """The one mapping resource an endpoint declares, with the path in the failure."""
    resources = mapping_resources(single_mapping(policy, path))
    assert len(resources) == 1, f"{path} performs {len(resources)} SubjectAccessReviews, expected one: {resources}"
    return resources[0]


def assert_authorized(response: Any, description: str) -> None:
    """Fail when kube-rbac-proxy refused the request; anything below it answered."""
    LOGGER.info("%s: %s %s", description, response.status_code, response.text[:200])
    assert response.status_code not in (401, 403), (
        f"{description} was refused by kube-rbac-proxy ({response.status_code}), so the policy does not authorize it "
        f"for this tenant: {response.text[:500]}"
    )


# --- the policy the instance mounts ----------------------------------------


def test_the_policy_declares_each_generation_path_once(policy: dict[str, Any]) -> None:
    """The generation paths are declared, and declared once each: a later duplicate is never reached."""
    paths = endpoint_paths(policy)
    for path in (GENERATION_PATH, GENERATION_WILDCARD_PATH):
        assert paths.count(path) == 1, f"the policy declares {path} {paths.count(path)} times; paths: {paths}"


def test_the_generation_creation_mapping_authorizes_evaluations_create(policy: dict[str, Any]) -> None:
    """POST .../collections/generation asks for `evaluations create` in the tenant, not a collection write."""
    mapping = single_mapping(policy, GENERATION_PATH)
    assert mapping["methods"] == GENERATION_CREATE_METHODS, (
        f"{GENERATION_PATH} maps {mapping['methods']}, expected {GENERATION_CREATE_METHODS}"
    )

    resource = resource_of(policy, GENERATION_PATH)
    assert (resource.get("rewrites") or {}).get("byHttpHeader", {}).get("name") == TENANT_HEADER
    assert resource_attributes(resource) == {
        "namespace": TENANT_NAMESPACE_REWRITE,
        "apiGroup": EVALHUB_API_GROUP,
        "resource": "evaluations",
        "verb": "create",
    }, f"{GENERATION_PATH} authorizes {resource_attributes(resource)}, expected evaluations create in the tenant"


def test_the_generation_lifecycle_mapping_authorizes_evaluations_by_method(policy: dict[str, Any]) -> None:
    """GET and DELETE .../generation/{id} ask for `evaluations <method>` in the tenant."""
    mapping = single_mapping(policy, GENERATION_WILDCARD_PATH)
    assert mapping["methods"] == GENERATION_LIFECYCLE_METHODS, (
        f"{GENERATION_WILDCARD_PATH} maps {mapping['methods']}, expected {GENERATION_LIFECYCLE_METHODS}"
    )

    resource = resource_of(policy, GENERATION_WILDCARD_PATH)
    assert (resource.get("rewrites") or {}).get("byHttpHeader", {}).get("name") == TENANT_HEADER
    assert resource_attributes(resource) == {
        "namespace": TENANT_NAMESPACE_REWRITE,
        "apiGroup": EVALHUB_API_GROUP,
        "resource": "evaluations",
        "verb": METHOD_VERB_REWRITE,
    }, f"{GENERATION_WILDCARD_PATH} authorizes {resource_attributes(resource)}, expected evaluations by method"


def test_the_generation_paths_precede_the_generic_collections_entry(policy: dict[str, Any]) -> None:
    """The specific entries come first, so a generation request is never authorized as a collection."""
    index = endpoint_index(policy)
    for path in (GENERATION_PATH, GENERATION_WILDCARD_PATH):
        assert path in index, f"{path} is not declared; paths: {endpoint_paths(policy)}"
    assert COLLECTIONS_WILDCARD_PATH in index, (
        f"{COLLECTIONS_WILDCARD_PATH} is not declared; paths: {endpoint_paths(policy)}"
    )

    wildcard_methods = single_mapping(policy, COLLECTIONS_WILDCARD_PATH)["methods"]
    shadowed = sorted(set(GENERATION_LIFECYCLE_METHODS) & set(wildcard_methods))
    assert shadowed, (
        f"{COLLECTIONS_WILDCARD_PATH} maps {wildcard_methods}, none of the generation lifecycle methods "
        f"{GENERATION_LIFECYCLE_METHODS}: the ordering this check is about would no longer matter"
    )
    LOGGER.info("%s methods %s overlap %s", COLLECTIONS_WILDCARD_PATH, wildcard_methods, shadowed)

    assert index[GENERATION_PATH] < index[COLLECTIONS_WILDCARD_PATH], (
        f"{GENERATION_PATH} is declared after {COLLECTIONS_WILDCARD_PATH}, whose {shadowed} methods would match it "
        "first"
    )
    assert index[GENERATION_WILDCARD_PATH] < index[COLLECTIONS_WILDCARD_PATH], (
        f"{GENERATION_WILDCARD_PATH} is declared after {COLLECTIONS_WILDCARD_PATH}, whose {shadowed} methods would "
        "match it first"
    )


def test_the_policy_maps_no_discovery_under_generation(policy: dict[str, Any]) -> None:
    """`.../collections/generation/models` was removed from the API and has no authorization mapping."""
    assert GENERATION_MODELS_PATH not in endpoint_index(policy), (
        f"the policy still declares {GENERATION_MODELS_PATH}, an endpoint the API no longer serves"
    )


def test_the_generic_collections_entry_is_unchanged(policy: dict[str, Any]) -> None:
    """The wildcard entry still authorizes `collections`: the generation entries were added, not swapped in."""
    mapping = single_mapping(policy, COLLECTIONS_WILDCARD_PATH)
    assert mapping["methods"] == ["get", "delete", "put", "patch"], (
        f"{COLLECTIONS_WILDCARD_PATH} maps {mapping['methods']}, expected the collection methods"
    )
    resource = resource_of(policy, COLLECTIONS_WILDCARD_PATH)
    assert resource_attributes(resource) == {
        "namespace": TENANT_NAMESPACE_REWRITE,
        "apiGroup": EVALHUB_API_GROUP,
        "resource": "collections",
        "verb": METHOD_VERB_REWRITE,
    }, f"{COLLECTIONS_WILDCARD_PATH} authorizes {resource_attributes(resource)}, expected collections by method"


def test_the_mounted_policy_is_the_one_the_pod_template_names(
    apps_client: client.AppsV1Api,
    evalhub_instance: dict[str, Any],
    evalhub_instance_namespace: str,
    policy_config_map: client.V1ConfigMap,
) -> None:
    """The pod template's digest annotation describes the policy the proxy container reads."""
    deployment = read_deployment(apps_client, name=EVALHUB_NAME, namespace=evalhub_instance_namespace)
    annotations = deployment.spec.template.metadata.annotations or {}
    recorded = annotations.get(AUTH_CONFIG_HASH_ANNOTATION)
    assert recorded, (
        f"deployment {evalhub_instance_namespace}/{EVALHUB_NAME} records no {AUTH_CONFIG_HASH_ANNOTATION}, so a policy "
        f"change would not roll the pod; annotations: {sorted(annotations)}"
    )

    mounted = (policy_config_map.data or {})[AUTH_CONFIG_KEY]
    assert auth_config_digest(mounted) == recorded, (
        f"deployment {evalhub_instance_namespace}/{EVALHUB_NAME} records {AUTH_CONFIG_HASH_ANNOTATION}={recorded}, but "
        f"the {AUTH_CONFIG_KEY} it mounts from {policy_config_map.metadata.name} hashes to "
        f"{auth_config_digest(mounted)}: the pod is running a policy the annotation does not describe"
    )


# --- the tenant Role the operator writes -----------------------------------


def test_the_tenant_role_covers_the_generation_lifecycle(tenant_role: client.V1Role) -> None:
    """The tenant-user grant has the verbs the generation policy maps requests to."""
    verbs = rule_verbs(tenant_role, "evaluations")
    LOGGER.info("%s grants evaluations %s", tenant_role.metadata.name, sorted(verbs))
    missing = GENERATION_VERBS - verbs
    assert not missing, (
        f"tenant Role {tenant_role.metadata.name} grants evaluations {sorted(verbs)}, so a generation request mapped "
        f"to evaluations would be refused for {sorted(missing)}"
    )


def test_the_tenant_role_grants_no_collection_writes(tenant_role: client.V1Role) -> None:
    """Generating a collection does not hand the tenant the right to write one."""
    verbs = rule_verbs(tenant_role, "collections")
    LOGGER.info("%s grants collections %s", tenant_role.metadata.name, sorted(verbs))
    writes = COLLECTION_WRITE_VERBS & verbs
    assert not writes, (
        f"tenant Role {tenant_role.metadata.name} grants collections {sorted(writes)}; the generation endpoints are "
        f"authorized as evaluations for exactly this reason"
    )


# --- the API behind the policy ---------------------------------------------
#
# The identity is granted the operator's tenant-user rules, so these calls pass
# only if the policy maps the path to a verb that grant carries: POST needs
# evaluations create, DELETE needs evaluations delete, and neither is a
# collection verb.


def test_the_generation_creation_path_is_authorized(evalhub_api_base: str, tenant_user_headers: dict[str, str]) -> None:
    """POST .../collections/generation reaches the API as `evaluations create`, not as a collection write."""
    response = request(
        "post",
        f"{evalhub_api_base}{GENERATION_PATH}",
        headers=tenant_user_headers,
        json_body={},
    )
    assert_authorized(response, f"POST {GENERATION_PATH} as the tenant user")


def test_the_generation_status_path_is_authorized(evalhub_api_base: str, tenant_user_headers: dict[str, str]) -> None:
    """GET .../collections/generation/{id} reaches the API as `evaluations get`."""
    generation_id = str(uuid.uuid4())
    response = request(
        "get",
        f"{evalhub_api_base}{GENERATION_PATH}/{generation_id}",
        headers=tenant_user_headers,
    )
    assert_authorized(response, f"GET {GENERATION_PATH}/{generation_id} as the tenant user")


def test_the_generation_cancellation_path_is_authorized(
    evalhub_api_base: str, tenant_user_headers: dict[str, str]
) -> None:
    """DELETE .../collections/generation/{id} reaches the API as `evaluations delete`, which the wildcard would not."""
    generation_id = str(uuid.uuid4())
    response = request(
        "delete",
        f"{evalhub_api_base}{GENERATION_PATH}/{generation_id}",
        headers=tenant_user_headers,
    )
    assert_authorized(response, f"DELETE {GENERATION_PATH}/{generation_id} as the tenant user")


def test_the_generation_paths_need_a_token(evalhub_api_base: str) -> None:
    """The generation paths are not exempt from authorization, unlike the health path."""
    response = request("post", f"{evalhub_api_base}{GENERATION_PATH}", json_body={})
    LOGGER.info("anonymous POST %s: %s", GENERATION_PATH, response.status_code)
    assert response.status_code in (401, 403), (
        f"POST {GENERATION_PATH} without a token answered {response.status_code}, expected to be refused"
    )


def test_the_generation_paths_are_scoped_to_the_named_tenant(
    evalhub_api_base: str,
    tenant_user_headers: dict[str, str],
    evalhub_instance_namespace: str,
) -> None:
    """The same token is refused when `X-Tenant` names a namespace it has no grant in."""
    headers = dict(tenant_user_headers)
    headers[TENANT_HEADER] = evalhub_instance_namespace
    generation_id = str(uuid.uuid4())
    response = request(
        "get",
        f"{evalhub_api_base}{GENERATION_PATH}/{generation_id}",
        headers=headers,
    )
    LOGGER.info("GET %s/%s as a foreign tenant: %s", GENERATION_PATH, generation_id, response.status_code)
    assert response.status_code == 403, (
        f"GET {GENERATION_PATH}/{generation_id} with {TENANT_HEADER}={evalhub_instance_namespace} answered "
        f"{response.status_code}; the tenant rewrite has to scope the SubjectAccessReview to the header's namespace"
    )
