import subprocess
from pathlib import Path

import pytest

from app.repository import Repository, RepositoryError


def git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "source"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test User")
    (path / "app.py").write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    (path / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0.1'\n", encoding="utf-8"
    )
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


def test_context_is_scoped_and_worktree_is_isolated(repo: Path, tmp_path: Path) -> None:
    source = Repository(repo, [tmp_path])
    context = source.collect_context(["app.py"], "greet", 10_000)
    worktree = source.create_worktree(tmp_path / "worktree", "feature/test")
    worktree.write_changes([("app.py", "def greet():\n    return 'changed'\n")])

    assert "--- FILE: app.py ---" in context
    assert "changed" not in (repo / "app.py").read_text(encoding="utf-8")
    assert "changed" in worktree.diff()


def test_search_falls_back_when_ripgrep_is_unavailable(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    (repo / "unfinished.py").write_text("STATUS = 'needs fallback search'\n", encoding="utf-8")
    monkeypatch.setattr("app.repository.shutil.which", lambda _: None)

    result = Repository(repo, [tmp_path]).search("fallback")

    assert "unfinished.py:1:STATUS = 'needs fallback search'" in result


def test_local_changes_become_an_isolated_clean_baseline(repo: Path, tmp_path: Path) -> None:
    staged = "def greet():\n    return 'partially implemented'\n"
    working = staged + "\ndef local_only():\n    return True\n"
    (repo / "app.py").write_text(staged, encoding="utf-8")
    git(repo, "add", "app.py")
    (repo / "app.py").write_text(working, encoding="utf-8")
    (repo / "helper.py").write_text("VALUE = 'local'\n", encoding="utf-8")
    source = Repository(repo, [tmp_path])
    original_status = source.status()
    original_staged_diff = git(repo, "diff", "--cached")

    worktree = source.create_worktree(tmp_path / "local-worktree", "feature/local")
    snapshot = source.copy_local_changes_to(worktree, 1_000_000)

    assert snapshot.files == ["app.py", "helper.py"]
    assert snapshot.baseline_commit
    assert (worktree.path / "app.py").read_text(encoding="utf-8") == working
    assert (worktree.path / "helper.py").read_text(encoding="utf-8") == "VALUE = 'local'\n"
    assert worktree.status() == ""
    assert worktree.diff() == ""
    assert source.status() == original_status
    assert git(repo, "diff", "--cached") == original_staged_diff

    worktree.write_changes(
        [
            ("app.py", working + "\ndef ai_result():\n    return 'done'\n"),
            ("helper.py", "VALUE = 'AI completed'\n"),
            ("result.py", "print('AI result')\n"),
        ]
    )
    assert worktree.delivery_files() == ["app.py", "helper.py", "result.py"]

    source.apply_patch(worktree.diff(), git(repo, "rev-parse", "HEAD"))

    assert "def ai_result" in (repo / "app.py").read_text(encoding="utf-8")
    assert (repo / "helper.py").read_text(encoding="utf-8") == "VALUE = 'AI completed'\n"
    assert (repo / "result.py").read_text(encoding="utf-8") == "print('AI result')\n"
    assert git(repo, "diff", "--cached") == original_staged_diff


def test_source_sync_rejects_conflicting_local_edits(repo: Path, tmp_path: Path) -> None:
    source = Repository(repo, [tmp_path])
    source_head = source.head_oid()
    worktree = source.create_worktree(tmp_path / "sync-worktree", "feature/sync")
    worktree.write_changes([("app.py", "def greet():\n    return 'AI'\n")])
    patch = worktree.diff()
    local_content = "def greet():\n    return 'user changed this while AI ran'\n"
    (repo / "app.py").write_text(local_content, encoding="utf-8")

    with pytest.raises(RepositoryError, match="changed or conflict"):
        source.apply_patch(patch, source_head)

    assert (repo / "app.py").read_text(encoding="utf-8") == local_content


def test_local_snapshot_rejects_oversized_untracked_files(repo: Path, tmp_path: Path) -> None:
    (repo / "large.bin").write_bytes(b"123456")
    source = Repository(repo, [tmp_path])
    worktree = source.create_worktree(tmp_path / "limited-worktree", "feature/limited")

    with pytest.raises(RepositoryError, match="snapshot size limit"):
        source.copy_local_changes_to(worktree, 5)


def test_local_snapshot_rejects_a_different_existing_branch(repo: Path, tmp_path: Path) -> None:
    git(repo, "branch", "feature/old")
    (repo / "README.md").write_text("new base\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "advance main")
    (repo / "app.py").write_text("print('local')\n", encoding="utf-8")
    source = Repository(repo, [tmp_path])
    worktree = source.create_worktree(tmp_path / "old-branch-worktree", "feature/old")

    with pytest.raises(RepositoryError, match="source repository HEAD"):
        source.copy_local_changes_to(worktree, 1_000_000)


def test_rejects_paths_outside_repository(repo: Path, tmp_path: Path) -> None:
    repository = Repository(repo, [tmp_path])
    with pytest.raises(RepositoryError, match="escapes repository"):
        repository.write_changes([("../escape.txt", "bad")])


def test_rejects_dangerous_commands(repo: Path, tmp_path: Path) -> None:
    repository = Repository(repo, [tmp_path])
    with pytest.raises(RepositoryError, match="safety policy"):
        repository.run_shell("git reset --hard", 5)


def test_empty_repository_uses_orphan_worktree(tmp_path: Path) -> None:
    source_path = tmp_path / "empty"
    source_path.mkdir()
    git(source_path, "init", "-b", "main")
    source = Repository(source_path, [tmp_path])

    worktree = source.create_worktree(tmp_path / "empty-worktree", "autoflow/first-task")
    worktree.write_changes([("hello.py", "print('hello world')\n")])

    assert worktree.current_branch() == "autoflow/first-task"
    assert source.current_branch() == "main"
    assert "hello.py" in worktree.status()
    assert source.status() == ""
    assert source.recent_history() == ""


def test_empty_repository_can_snapshot_local_source_files(tmp_path: Path) -> None:
    source_path = tmp_path / "empty-local"
    source_path.mkdir()
    git(source_path, "init", "-b", "main")
    (source_path / "partial.py").write_text("VALUE = 'draft'\n", encoding="utf-8")
    source = Repository(source_path, [tmp_path])

    worktree = source.create_worktree(tmp_path / "empty-local-worktree", "autoflow/continue-draft")
    snapshot = source.copy_local_changes_to(worktree, 1_000_000)

    assert snapshot.files == ["partial.py"]
    assert snapshot.baseline_commit
    assert worktree.status() == ""
    assert (worktree.path / "partial.py").read_text(encoding="utf-8") == "VALUE = 'draft'\n"
    assert source.status().strip() == "?? partial.py"


def test_hygiene_findings_require_generated_files_to_be_ignored(repo: Path, tmp_path: Path) -> None:
    repository = Repository(repo, [tmp_path])
    cache = repo / "__pycache__"
    cache.mkdir()
    (cache / "app.pyc").write_bytes(b"compiled")

    assert "__pycache__/app.pyc" in repository.hygiene_findings()[0]

    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    assert repository.hygiene_findings() == []
