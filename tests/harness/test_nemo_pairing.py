"""The rules that keep a NeMo check honest: which Dockerfile is built, and how NEMO_IMG pairs with NEMO_REPO.

A green `nemo` check has to mean the operator ran an image this run built, or an image the run
named on purpose. These tests hold the harness to the pairings that make that true.
"""

from __future__ import annotations

import pytest

from harness import deploy, images, registry
from harness.config import Settings
from harness.shell import HarnessError


def test_the_nemo_image_is_built_from_the_operator_containerfile(monkeypatch, tmp_path):
    (tmp_path / "Dockerfile.server").write_text("FROM scratch\n")
    built = []
    monkeypatch.setattr(
        images, "build", lambda settings, image, context, containerfile, *args: built.append(containerfile)
    )
    images.build_nemo(Settings(nemo_repo=str(tmp_path)), "localhost/nemo-guardrails-server:dev")
    assert built == [tmp_path / "Dockerfile.server"]


def test_a_checkout_with_only_the_upstream_dockerfile_is_refused(monkeypatch, tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python\n")
    monkeypatch.setattr(images, "build", lambda *args, **kwargs: pytest.fail("built the upstream Dockerfile"))
    with pytest.raises(HarnessError, match="Dockerfile.server"):
        images.build_nemo(Settings(nemo_repo=str(tmp_path)), "localhost/nemo-guardrails-server:dev")


def test_images_all_refuses_a_named_nemo_image_beside_its_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(images, "require_cluster", lambda settings: None)
    monkeypatch.setattr(images, "require_operator_repo", lambda settings: None)
    monkeypatch.setattr(images.registry, "ensure", lambda settings: pytest.fail("the registry was started"))
    settings = Settings(
        nemo_repo=str(tmp_path),
        nemo_img="quay.io/me/nemo:mine",
        explicit=frozenset({"nemo_img"}),
    )
    with pytest.raises(HarnessError, match="NEMO_IMG cannot be named with NEMO_REPO"):
        images.images_all(settings)


def test_deploy_refuses_a_named_nemo_image_that_differs_from_the_built_one(monkeypatch, tmp_path):
    record = tmp_path / "images.env"
    record.write_text("NEMO_IMG=localhost:5005/nemo-guardrails-server:dev\n")
    monkeypatch.setattr(deploy, "deploy", lambda settings: pytest.fail("deployed a different image"))
    settings = Settings(
        images_env_file=str(record),
        nemo_img="quay.io/me/nemo:mine",
        explicit=frozenset({"nemo_img"}),
    )
    with pytest.raises(HarnessError, match="differs from the server images-all built"):
        deploy.registry_deploy(settings)


def test_deploy_takes_the_built_nemo_image_when_none_is_named(monkeypatch, tmp_path):
    record = tmp_path / "images.env"
    record.write_text("NEMO_IMG=localhost:5005/nemo-guardrails-server:dev\n")
    deployed = []
    monkeypatch.setattr(deploy, "deploy", lambda settings: deployed.append(settings.nemo_img))
    deploy.registry_deploy(Settings(images_env_file=str(record)))
    assert deployed == ["localhost:5005/nemo-guardrails-server:dev"]


def test_builds_lmes_and_selected_adapters_read_the_same_rule():
    for settings in (Settings(adapters="none lmes"), Settings(adapters="lmes none")):
        with pytest.raises(HarnessError, match="stands alone"):
            registry.selected_adapters(settings)
        with pytest.raises(HarnessError, match="stands alone"):
            registry.builds_lmes(settings)


def test_an_empty_adapters_value_means_all_to_every_reader():
    settings = Settings(adapters="")
    assert registry.selected_adapters(settings) == list(registry.CONTRIB_ADAPTERS)
    assert registry.builds_lmes(settings)
