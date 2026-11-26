"""The NeMo Guardrails server: its settings, its build, the record it is deployed from, and its section."""

from __future__ import annotations

import pytest

from harness import deploy, images, registry, tests
from harness.config import Settings
from harness.shell import HarnessError

NEMO_RELATED = "RELATED_IMAGE_ODH_TRUSTYAI_NEMO_GUARDRAILS_SERVER_IMAGE"
NEMO_RECORDED = "localhost:5005/nemo-guardrails-server:dev"


def test_the_nemo_build_is_opt_in():
    assert Settings().nemo_repo == ""


def test_the_nemo_section_names_its_marker():
    assert "nemo" in tests.SECTIONS
    assert tests.section_marker("nemo", Settings()) == "nemo"


def test_the_record_fills_an_unset_nemo_image():
    assert Settings().with_image_record({"NEMO_IMG": NEMO_RECORDED}).nemo_img == NEMO_RECORDED


def test_a_nemo_image_named_by_the_caller_survives_the_record():
    named = Settings(nemo_img="quay.io/me/nemo:mine", explicit=frozenset({"nemo_img"}))
    assert named.with_image_record({"NEMO_IMG": NEMO_RECORDED}).nemo_img == "quay.io/me/nemo:mine"


def test_the_nemo_server_image_is_handed_over_as_its_related_image():
    settings = Settings(nemo_img="quay.io/me/nemo:mine")
    assert dict(deploy.related_image_env(settings))[NEMO_RELATED] == "quay.io/me/nemo:mine"
    assert deploy.operand_images(settings)["nemo-guardrails-image"] == "quay.io/me/nemo:mine"


def test_without_a_nemo_image_the_operator_keeps_its_pin():
    assert NEMO_RELATED not in dict(deploy.related_image_env(Settings()))
    assert "nemo-guardrails-image" not in deploy.operand_images(Settings())


def test_the_nemo_build_needs_its_dockerfile(tmp_path):
    with pytest.raises(HarnessError, match="Dockerfile"):
        images.build_nemo(Settings(nemo_repo=str(tmp_path)), NEMO_RECORDED)


def test_a_missing_nemo_checkout_stops_images_all_before_the_registry_starts(monkeypatch, tmp_path):
    monkeypatch.setattr(images, "require_cluster", lambda settings: None)
    monkeypatch.setattr(images, "require_operator_repo", lambda settings: None)
    monkeypatch.setattr(images.registry, "ensure", lambda settings: pytest.fail("the registry was started"))
    with pytest.raises(HarnessError, match="NEMO_REPO"):
        images.images_all(Settings(nemo_repo=str(tmp_path / "absent")))


def test_images_all_builds_the_nemo_image_only_when_its_checkout_is_named(monkeypatch, tmp_path):
    checkout = tmp_path / "NeMo-Guardrails"
    checkout.mkdir()
    built, pushed = [], []
    monkeypatch.setattr(images, "require_cluster", lambda settings: None)
    monkeypatch.setattr(images, "require_operator_repo", lambda settings: None)
    monkeypatch.setattr(images, "deploys_module_operator", lambda settings: False)
    monkeypatch.setattr(images.registry, "ensure", lambda settings: None)
    monkeypatch.setattr(images.registry, "configure_nodes", lambda settings: None)
    monkeypatch.setattr(images, "build_operators", lambda settings: None)
    monkeypatch.setattr(images, "build_evalhub", lambda settings, image: None)
    monkeypatch.setattr(images, "build_nemo", lambda settings, image: built.append(image))
    monkeypatch.setattr(images, "push", lambda settings, image: pushed.append(image))

    record = tmp_path / "named.env"
    images.images_all(Settings(images_env_file=str(record), adapters="none", nemo_repo=str(checkout)))
    assert [image for image in built if "nemo-guardrails-server" in image] == [
        "localhost:5005/nemo-guardrails-server:dev"
    ]
    assert f"NEMO_IMG={NEMO_RECORDED}" in record.read_text()
    assert NEMO_RECORDED in pushed

    built.clear()
    pushed.clear()
    unnamed = tmp_path / "unnamed.env"
    images.images_all(Settings(images_env_file=str(unnamed), adapters="none"))
    assert not any("nemo-guardrails-server" in image for image in built + pushed)
    assert "NEMO_IMG" not in unnamed.read_text()


def test_adapters_none_builds_no_adapter():
    assert registry.selected_adapters(Settings(adapters="none")) == []
    assert not registry.builds_lmes(Settings(adapters="none"))


def test_adapters_none_stands_alone():
    with pytest.raises(HarnessError, match="stands alone"):
        registry.selected_adapters(Settings(adapters="none lmes"))
