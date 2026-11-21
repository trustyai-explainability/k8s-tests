"""Shared pytest fixtures.

The tests talk to a live cluster, by default the Kind cluster the harness deploys
the operators into. Override the target with KUBE_CONTEXT and
NAMESPACE when running against a different cluster or namespace.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient

from utilities.kube import KEEP_RESOURCES_VAR, context_from_env, keep_resources, load_clients, namespace_from_env
from utilities.release import RELEASE_MARKER, RELEASE_OPTION, RELEASE_VAR, declared_release
from utilities.release import applies as release_applies
from utilities.release import describe as describe_release
from utilities.release import parse as parse_release

LOGGER = logging.getLogger(__name__)

# The report is named after the project, not after the file pytest-html writes it
# to, and it is prefixed with the project logo.
REPORT_TITLE = "k8s-tests"
REPORT_LOGO = Path(__file__).resolve().parent / "docs" / "trustyai.png"
_TEMP_DIRS: list[Path] = []


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--keep-resources",
        action="store_true",
        default=False,
        help="leave the namespaces, the LLM emulator and the job resources on the cluster for inspection",
    )
    parser.addoption(
        RELEASE_OPTION,
        action="store",
        default=None,
        metavar="RELEASE",
        help=(
            "RHOAI/ODH release the operator under test is from, for example 3.5 or 3.6; "
            f"checks whose {RELEASE_MARKER} range excludes it are skipped (defaults to ${RELEASE_VAR})"
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    """`--keep-resources` and KEEP_RESOURCES=1 do the same thing, so the fixtures check one place."""
    if config.getoption("keep_resources"):
        os.environ[KEEP_RESOURCES_VAR] = "1"
    # The flag and the environment variable are one declaration: the harness exports
    # OPERATOR_RELEASE, and the utilities read it from there.
    declared = config.getoption("release")
    if declared:
        # Fail the run, not one check, when the declaration cannot be compared to a
        # range: every gate would otherwise silently disagree with it.
        try:
            parse_release(str(declared))
        except ValueError as exc:
            raise pytest.UsageError(f"{RELEASE_OPTION}: {exc}") from exc
        os.environ[RELEASE_VAR] = str(declared).strip()
    if config.option.htmlpath:
        _install_report_logo(config)


def pytest_unconfigure(config: pytest.Config) -> None:
    for directory in _TEMP_DIRS:
        shutil.rmtree(directory, ignore_errors=True)
    _TEMP_DIRS.clear()


def pytest_html_report_title(report: object) -> None:
    """Name the report after the project instead of the file it is written to."""
    report.title = REPORT_TITLE


def _install_report_logo(config: pytest.Config) -> None:
    """Prefix the report title with the logo, embedded so the file stays portable."""
    if not REPORT_LOGO.exists():
        LOGGER.warning("no report logo at %s; the title will be written without one", REPORT_LOGO)
        return
    encoded = base64.b64encode(REPORT_LOGO.read_bytes()).decode()
    css = (
        "/* k8s-tests: the project logo before the report title. */\n"
        "h1#title::before {\n"
        '  content: "";\n'
        "  display: inline-block;\n"
        "  width: 2.2em;\n"
        "  height: 2.2em;\n"
        "  margin-right: 0.4em;\n"
        "  vertical-align: middle;\n"
        '  background: url("data:image/png;base64,'
        f'{encoded}") no-repeat center / contain;\n'
        "}\n"
    )
    directory = Path(tempfile.mkdtemp(prefix="k8s-tests-report-"))
    _TEMP_DIRS.append(directory)
    path = directory / "report-logo.css"
    path.write_text(css)
    # pytest-html appends the content of every --css file to the report style, so
    # the logo is inlined into the self-contained report rather than linked.
    config.option.css.append(str(path))
    LOGGER.info("prefixing the report title with %s", REPORT_LOGO)


@pytest.fixture(scope="session")
def kube_context() -> str:
    """Kubernetes context the tests run against."""
    return context_from_env()


@pytest.fixture(scope="session")
def namespace() -> str:
    """Namespace the TrustyAI operators are deployed into."""
    return namespace_from_env()


@pytest.fixture(scope="session")
def clients(kube_context: str) -> tuple[client.CoreV1Api, client.AppsV1Api]:
    """Core and Apps API clients for the target cluster."""
    return load_clients(context=kube_context)


@pytest.fixture(scope="session")
def core_client(clients: tuple[client.CoreV1Api, client.AppsV1Api]) -> client.CoreV1Api:
    return clients[0]


@pytest.fixture(scope="session")
def apps_client(clients: tuple[client.CoreV1Api, client.AppsV1Api]) -> client.AppsV1Api:
    return clients[1]


@pytest.fixture(scope="session")
def api_client(core_client: client.CoreV1Api) -> client.ApiClient:
    """The raw API client the typed clients share, for discovery-style APIs."""
    return core_client.api_client


@pytest.fixture(scope="session")
def custom_api(api_client: client.ApiClient) -> client.CustomObjectsApi:
    """Client for the custom resources the tests create and read."""
    return client.CustomObjectsApi(api_client)


@pytest.fixture(scope="session")
def rbac_client(api_client: client.ApiClient) -> client.RbacAuthorizationV1Api:
    """Client for the Roles and RoleBindings the API checks grant."""
    return client.RbacAuthorizationV1Api(api_client)


@pytest.fixture(scope="session")
def networking_client(api_client: client.ApiClient) -> client.NetworkingV1Api:
    """Client for the NetworkPolicies the workload operator creates for a workload."""
    return client.NetworkingV1Api(api_client)


@pytest.fixture(scope="session")
def dynamic_client(api_client: client.ApiClient) -> DynamicClient:
    """Dynamic client for custom resources the typed clients do not model."""
    return DynamicClient(api_client)


@pytest.fixture(scope="session", autouse=True)
def report_target(clients: tuple[client.CoreV1Api, client.AppsV1Api], kube_context: str, namespace: str) -> None:
    """Log what the session is pointed at, so the report says where it ran."""
    core_client, _ = clients
    LOGGER.info(
        "target: context=%s namespace=%s server=%s",
        kube_context,
        namespace,
        core_client.api_client.configuration.host,
    )
    declared = declared_release()
    if declared:
        LOGGER.info("release: %s; checks declaring a range without it are skipped", declared)
    else:
        LOGGER.info(
            "release: not declared; set OPERATOR_RELEASE (or %s) to gate the checks by release",
            RELEASE_OPTION,
        )
    if keep_resources():
        LOGGER.warning(
            "KEEP_RESOURCES is set: teardown will leave the EvalHub instance, its tenant namespace, the "
            "evaluation job and the LLM emulator on the cluster for inspection"
        )
        LOGGER.warning(
            "a re-run will find them: every instance is named 'evalhub' and keeps reconciling every "
            "tenant-labelled namespace, and the namespaces accumulate. Delete them before a clean run: "
            "kubectl get ns -o name | grep trustyai-evalhub | xargs -r kubectl delete, and "
            "kubectl -n %s delete deploy,svc -l app=vllm-emulator",
            namespace,
        )
    try:
        version = client.VersionApi(core_client.api_client).get_code()
    except ApiException as exc:  # a version probe must not fail the run
        LOGGER.warning("could not read the server version: %s", exc.reason)
    else:
        LOGGER.info("server version: %s", version.git_version)


# --- report grouping -------------------------------------------------------
#
# Selection has two axes: the directory names the component (`tests/evalhub`), the
# marker names the section within it (`generation`, `benchmark`, `adapters`). A
# module therefore carries its component's marker plus its section's, so it is
# selected as part of the component and can still be run on its own.
#
# The report groups by the first marker in this list, so a section that has to stay
# a block of its own (`adapters`) comes before the component it belongs to. The run
# is ordered the same way, and the Scope column names the block a row starts.

SCOPE_MARKERS = ("adapters", "evalhub", "emulator", "lmeval", "module", "service", "workload")
SCOPE_TITLES = {
    "adapters": "EvalHub adapters",
    "evalhub": "EvalHub",
    "emulator": "LLM emulator",
    "lmeval": "LMEvalJob",
    "module": "Module operator",
    "service": "TrustyAI service",
    "workload": "Workload operator",
}
SCOPE_UNMARKED = "other"

_SCOPES: dict[str, str] = {}
_SCOPE_STARTS: set[str] = set()
_OUTCOMES: dict[str, str] = {}


def _scope_of(item: pytest.Item) -> str:
    for marker in SCOPE_MARKERS:
        if item.get_closest_marker(marker):
            return marker
    return SCOPE_UNMARKED


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Order the run by scope so the report groups the tests by component, and apply the version gates."""
    _skip_outside_declared_releases(items)
    order = {scope: index for index, scope in enumerate((*SCOPE_MARKERS, SCOPE_UNMARKED))}
    for item in items:
        _SCOPES[item.nodeid] = _scope_of(item)
    items.sort(key=lambda item: order[_SCOPES[item.nodeid]])
    seen: set[str] = set()
    for item in items:
        scope = _SCOPES[item.nodeid]
        if scope not in seen:
            _SCOPE_STARTS.add(item.nodeid)
            seen.add(scope)


def _skip_outside_declared_releases(items: list[pytest.Item]) -> None:
    """Skip the checks whose declared release range excludes the one this run targets.

    Without a declared release nothing is skipped: the run keeps every check and the
    fixtures skip the ones whose subject the cluster does not have.
    """
    run = declared_release()
    if run is None:
        return
    for item in items:
        mark = item.get_closest_marker(RELEASE_MARKER)
        if mark is None:
            continue
        lower, upper = mark.kwargs.get("min"), mark.kwargs.get("max")
        try:
            if release_applies(min=lower, max=upper, run=run):
                continue
            requirement = describe_release(min=lower, max=upper)
        except ValueError as exc:
            raise pytest.UsageError(f"{item.nodeid}: {exc}") from exc
        item.add_marker(pytest.mark.skip(reason=f"applies to operator releases {requirement}; this run targets {run}"))


def pytest_html_results_table_header(cells: list[str]) -> None:
    """Add the Scope column to the HTML report's results table."""
    cells.insert(1, "<th>Scope</th>")


def pytest_html_results_table_row(report: pytest.TestReport, cells: list[str]) -> None:
    """Name the scope on the first row of each block, separate the blocks."""
    scope = _SCOPES.get(report.nodeid, SCOPE_UNMARKED)
    title = SCOPE_TITLES.get(scope, scope)
    if report.nodeid in _SCOPE_STARTS:
        cells.insert(
            1,
            f'<td class="col-scope" style="border-top:2px solid #6c757d; white-space:nowrap;">'
            f"<strong>{title}</strong></td>",
        )
    else:
        cells.insert(1, '<td class="col-scope"></td>')


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record one outcome per test, so the summary counts what actually ran."""
    if report.when == "call":
        _OUTCOMES[report.nodeid] = report.outcome
    elif report.when == "setup" and report.outcome != "passed":
        _OUTCOMES.setdefault(report.nodeid, report.outcome)


def pytest_html_results_summary(
    prefix: list[str], summary: list[str], postfix: list[str], session: pytest.Session
) -> None:
    """Say how the report is grouped, above the results table."""
    counts: dict[str, dict[str, int]] = {}
    for nodeid, outcome in _OUTCOMES.items():
        scope = _SCOPES.get(nodeid, SCOPE_UNMARKED)
        counts.setdefault(scope, {})
        counts[scope][outcome] = counts[scope].get(outcome, 0) + 1

    def describe(scope: str) -> str:
        outcomes = counts[scope]
        parts = [
            f"{outcomes[outcome]} {outcome}" for outcome in ("passed", "failed", "skipped") if outcomes.get(outcome)
        ]
        title = SCOPE_TITLES.get(scope, scope)
        return f"{title} ({', '.join(parts)})"

    declared = declared_release()
    if declared:
        prefix.append(
            f'<p class="col-operator-release">Operator release under test: <code>{declared}</code>; '
            "checks declaring a different range were skipped.</p>"
        )
    blocks = [describe(scope) for scope in (*SCOPE_MARKERS, SCOPE_UNMARKED) if counts.get(scope)]
    if blocks:
        prefix.append(f'<p class="col-scope-summary">Grouped by scope: {"; ".join(blocks)}</p>')
    if keep_resources():
        prefix.append(
            '<p class="col-keep-resources">KEEP_RESOURCES is set: the namespaces, the LLM emulator and the job '
            "resources were left on the cluster. <code>kubectl get ns | grep trustyai-evalhub</code> shows which "
            "ones. A re-run finds them again - the instance keeps reconciling every tenant-labelled namespace, "
            "so delete them first if you want a clean run.</p>"
        )
