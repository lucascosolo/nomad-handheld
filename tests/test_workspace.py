"""The D15 workspace boundary. Security-critical — these are the tests that
have to keep passing when someone "simplifies" `Workspace.resolve`."""

from __future__ import annotations

from pathlib import Path

import pytest

from nomad.core.errors import PermissionDenied
from nomad.tools.workspace import Workspace


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace_root = tmp_path / "workspace"
    (workspace_root / "sub").mkdir(parents=True)
    (workspace_root / "sub" / "file.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    return workspace_root


@pytest.fixture
def workspace(root: Path) -> Workspace:
    return Workspace(root)


def test_resolves_a_relative_path_inside_the_root(workspace: Workspace, root: Path) -> None:
    assert workspace.resolve("sub/file.txt") == (root / "sub" / "file.txt").resolve()


def test_resolves_the_root_itself(workspace: Workspace, root: Path) -> None:
    assert workspace.resolve(".") == root.resolve()
    assert workspace.relative(".") == "."


def test_resolves_a_path_that_does_not_exist_yet(workspace: Workspace, root: Path) -> None:
    # Writes need this: the file is created after the check.
    assert workspace.resolve("sub/new/deep.txt") == (root / "sub" / "new" / "deep.txt")


def test_rejects_dotdot_traversal(workspace: Workspace) -> None:
    with pytest.raises(PermissionDenied):
        workspace.resolve("../outside/secret.txt")


def test_rejects_deeply_buried_dotdot_traversal(workspace: Workspace) -> None:
    with pytest.raises(PermissionDenied):
        workspace.resolve("sub/../../outside/secret.txt")


def test_rejects_an_absolute_path_outside_the_root(workspace: Workspace) -> None:
    with pytest.raises(PermissionDenied):
        workspace.resolve("/etc/passwd")


def test_accepts_an_absolute_path_inside_the_root(workspace: Workspace, root: Path) -> None:
    absolute = root / "sub" / "file.txt"
    assert workspace.resolve(absolute) == absolute.resolve()


def test_rejects_a_symlink_pointing_outside_the_root(
    workspace: Workspace, root: Path, tmp_path: Path
) -> None:
    link = root / "escape.txt"
    link.symlink_to(tmp_path / "outside" / "secret.txt")
    with pytest.raises(PermissionDenied):
        workspace.resolve("escape.txt")


def test_rejects_a_path_whose_parent_directory_is_a_symlink_outside(
    workspace: Workspace, root: Path, tmp_path: Path
) -> None:
    # The interesting case for writes: the file does not exist, but its parent
    # is a door out of the workspace.
    (root / "escape_dir").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(PermissionDenied):
        workspace.resolve("escape_dir/new_file.txt")


def test_allows_a_symlink_that_stays_inside_the_root(workspace: Workspace, root: Path) -> None:
    link = root / "alias.txt"
    link.symlink_to(root / "sub" / "file.txt")
    assert workspace.resolve("alias.txt") == (root / "sub" / "file.txt").resolve()


def test_follow_symlinks_outside_root_relaxes_only_the_symlink_rule(
    root: Path, tmp_path: Path
) -> None:
    permissive = Workspace(root, follow_symlinks_outside_root=True)
    (root / "escape.txt").symlink_to(tmp_path / "outside" / "secret.txt")

    # The opt-in flag allows the symlink...
    assert permissive.resolve("escape.txt") == root.resolve() / "escape.txt"
    # ...but must not re-open `..` traversal or absolute-path escape.
    with pytest.raises(PermissionDenied):
        permissive.resolve("../outside/secret.txt")
    with pytest.raises(PermissionDenied):
        permissive.resolve("/etc/passwd")


def test_rejects_a_null_byte(workspace: Workspace) -> None:
    with pytest.raises(PermissionDenied):
        workspace.resolve("sub/file\x00.txt")


def test_contains_reports_instead_of_raising(workspace: Workspace) -> None:
    assert workspace.contains("sub/file.txt") is True
    assert workspace.contains("../outside/secret.txt") is False


def test_root_is_resolved_at_construction(tmp_path: Path) -> None:
    # A workspace reached through a symlink must still match its own contents.
    real = tmp_path / "real_root"
    real.mkdir()
    (real / "f.txt").write_text("x")
    link = tmp_path / "link_root"
    link.symlink_to(real, target_is_directory=True)

    workspace = Workspace(link)
    assert workspace.root == real.resolve()
    assert workspace.resolve("f.txt") == (real / "f.txt").resolve()


def test_ensure_exists_creates_only_the_root(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fresh" / "nested")
    workspace.ensure_exists()
    assert workspace.root.is_dir()
