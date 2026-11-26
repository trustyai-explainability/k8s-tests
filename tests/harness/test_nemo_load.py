"""The NeMo server on the load path: built from NEMO_REPO, loaded onto the node, named for the deploy."""

from __future__ import annotations

import pytest

from harness import images
from harness.config import Settings
from harness.shell import HarnessError


def _stub_operator_builds(monkeypatch, tmp_path):
    """Keep the operator builds out of these tests; the NeMo build is what they exercise."""
    monkeypatch.setattr(images, "build_operators", lambda settings: None)
    monkeypatch.setattr(images, "loadable_adapter_images", lambda settings: [])


def test_the_load_path_builds_the_nemo_image_under_the_name_the_deploy_uses(monkeypatch, tmp_path):
    _stub_operator_builds(monkeypatch, tmp_path)
    built = []
    monkeypatch.setattr(images, "build_nemo", lambda settings, image: built.append(image))
    settings = Settings(
        images_env_file=str(tmp_path / "images.env"),
        nemo_repo=str(tmp_path),
        nemo_img="localhost/nemo-guardrails-server:dev",
    )
    images.build_load_path_images(settings)
    assert built == ["localhost/nemo-guardrails-server:dev"]


def test_the_load_path_refuses_a_nemo_checkout_without_an_image_name(monkeypatch, tmp_path):
    _stub_operator_builds(monkeypatch, tmp_path)
    monkeypatch.setattr(images, "build_nemo", lambda settings, image: pytest.fail("built without a name"))
    settings = Settings(images_env_file=str(tmp_path / "images.env"), nemo_repo=str(tmp_path))
    with pytest.raises(HarnessError, match="NEMO_IMG"):
        images.build_load_path_images(settings)


def test_the_load_path_builds_no_nemo_image_without_its_checkout(monkeypatch, tmp_path):
    _stub_operator_builds(monkeypatch, tmp_path)
    monkeypatch.setattr(images, "build_nemo", lambda settings, image: pytest.fail("built without NEMO_REPO"))
    images.build_load_path_images(Settings(images_env_file=str(tmp_path / "images.env")))


def test_a_named_nemo_image_is_one_the_load_path_puts_on_the_node():
    assert ("NeMo Guardrails server", "localhost/nemo-guardrails-server:dev") in images.named_images(
        Settings(nemo_img="localhost/nemo-guardrails-server:dev")
    )
    assert not any(label == "NeMo Guardrails server" for label, _ in images.named_images(Settings()))
