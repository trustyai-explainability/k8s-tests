"""Which layout a checkout has, and when a run refuses it."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.arch import (
    ARCH_MODULAR,
    ARCH_SINGLE,
    arch_source,
    operator_arch,
    operator_images,
    release_disagrees,
    require_operator_repo,
    single_operator_overlay,
)
from harness.config import Settings
from harness.shell import HarnessError


def checkout(root: Path, *, modular: bool, overlay: str = "config/overlays/rhoai") -> Path:
    """A directory that looks like a trustyai-service-operator checkout."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "Dockerfile").write_text("FROM scratch\n")
    (root / "config" / "default").mkdir(parents=True, exist_ok=True)
    (root / "config" / "default" / "kustomization.yaml").write_text("resources: []\n")
    if overlay:
        (root / overlay).mkdir(parents=True, exist_ok=True)
        (root / overlay / "kustomization.yaml").write_text("resources: []\n")
    if modular:
        (root / "trustyai-operator-module").mkdir()
        (root / "trustyai-operator-module" / "Dockerfile").write_text("FROM scratch\n")
    return root


def settings_for(root: Path | None, **overrides: str) -> Settings:
    return Settings(operator_repo=str(root) if root else "", **overrides)


def test_a_checkout_with_the_module_tree_is_modular(tmp_path):
    assert operator_arch(settings_for(checkout(tmp_path / "modular", modular=True))) == ARCH_MODULAR


def test_a_checkout_without_it_is_single(tmp_path):
    assert operator_arch(settings_for(checkout(tmp_path / "rhoai-3.5", modular=False))) == ARCH_SINGLE


def test_the_declared_layout_wins_over_the_checkout(tmp_path):
    root = checkout(tmp_path / "rhoai-3.5", modular=False)
    assert operator_arch(settings_for(root, operator_arch=ARCH_SINGLE)) == ARCH_SINGLE


def test_the_release_does_not_decide_the_layout(tmp_path):
    """`--release` is the release under test; only the tree decides what can be deployed."""
    single = checkout(tmp_path / "rhoai-3.5", modular=False)
    modular = checkout(tmp_path / "modular", modular=True)
    assert operator_arch(settings_for(single, release="3.6")) == ARCH_SINGLE
    assert operator_arch(settings_for(modular, release="3.5")) == ARCH_MODULAR


@pytest.mark.parametrize(
    ("modular", "expected"),
    [(True, "the checkout, which has trustyai-operator-module/"), (False, "the checkout, which has no")],
)
def test_the_source_says_what_chose_the_layout(tmp_path, modular, expected):
    root = checkout(tmp_path / "c", modular=modular)
    assert expected in arch_source(settings_for(root))
    assert arch_source(settings_for(root, operator_arch="single")) == "OPERATOR_ARCH=single"


def test_the_module_image_is_built_only_for_the_modular_layout(tmp_path):
    modular = settings_for(checkout(tmp_path / "m", modular=True))
    assert operator_images(modular) == [modular.workload_img, modular.module_img]

    single = settings_for(checkout(tmp_path / "s", modular=False))
    assert operator_images(single) == [single.workload_img]


def test_a_missing_checkout_is_reported_before_anything_is_built(tmp_path):
    with pytest.raises(HarnessError, match="OPERATOR_REPO must point"):
        require_operator_repo(settings_for(None))
    with pytest.raises(HarnessError, match="does not exist"):
        require_operator_repo(settings_for(tmp_path / "absent"))

    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(HarnessError, match="has no Dockerfile"):
        require_operator_repo(settings_for(plain))


def test_a_pre_module_checkout_is_accepted_without_a_module_tree(tmp_path):
    """The regression this started from: rhoai-3.5 has one Dockerfile and no module tree."""
    root = checkout(tmp_path / "rhoai-3.5", modular=False)
    assert require_operator_repo(settings_for(root)) == root


def test_a_pre_module_checkout_needs_an_overlay_to_render(tmp_path):
    root = checkout(tmp_path / "rhoai-3.5", modular=False, overlay="")
    (root / "config" / "default" / "kustomization.yaml").unlink()
    with pytest.raises(HarnessError, match="no config/overlays/rhoai"):
        require_operator_repo(settings_for(root))


def test_asking_for_the_modular_layout_on_a_pre_module_checkout_says_what_is_missing(tmp_path):
    root = checkout(tmp_path / "rhoai-3.5", modular=False)
    with pytest.raises(HarnessError, match="has no trustyai-operator-module/"):
        require_operator_repo(settings_for(root, operator_arch=ARCH_MODULAR))


def test_asking_for_the_single_layout_on_a_modular_checkout_is_refused(tmp_path):
    root = checkout(tmp_path / "modular", modular=True)
    with pytest.raises(HarnessError, match="the single layout is for a pre-module checkout"):
        require_operator_repo(settings_for(root, operator_arch=ARCH_SINGLE))


def test_a_half_synced_module_tree_is_reported(tmp_path):
    root = checkout(tmp_path / "modular", modular=True)
    (root / "trustyai-operator-module" / "Dockerfile").unlink()
    with pytest.raises(HarnessError, match="half-synced"):
        require_operator_repo(settings_for(root))


def test_the_single_layout_prefers_the_rhoai_overlay_and_falls_back(tmp_path):
    root = checkout(tmp_path / "odh", modular=False, overlay="config/overlays/odh")
    assert single_operator_overlay(settings_for(root)).name == "odh"

    (root / "config" / "overlays").rename(root / "elsewhere")
    assert single_operator_overlay(settings_for(root)) == root / "config" / "default"


@pytest.mark.parametrize(
    ("modular", "release", "expected"),
    [
        (True, "3.6", ""),
        (True, "3.5", "the layout follows the checkout"),
        (False, "3.5", ""),
        (False, "3.6", "the layout follows the checkout"),
        (True, "", ""),
    ],
)
def test_a_release_that_contradicts_the_layout_is_reported(tmp_path, modular, release, expected):
    root = checkout(tmp_path / "c", modular=modular)
    disagreeing = release_disagrees(settings_for(root, release=release))
    if expected:
        assert expected in disagreeing
    else:
        assert disagreeing == "", f"nothing to report, got {disagreeing!r}"


def test_an_unparsable_release_is_an_error_not_a_layout(tmp_path):
    root = checkout(tmp_path / "c", modular=True)
    with pytest.raises(HarnessError, match="not a release version"):
        release_disagrees(settings_for(root, release="3.x"))
