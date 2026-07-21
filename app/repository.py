from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

IGNORED_PARTS = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "target",
    "__pycache__",
    ".idea",
    ".vscode",
    "vendor",
}
TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".py",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".vue",
    ".svelte",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".md",
    ".txt",
    ".sql",
    ".sh",
    ".cmake",
    ".gradle",
    ".properties",
}
SPECIAL_TEXT_FILES = {
    "CMakeLists.txt",
    "Makefile",
    "Dockerfile",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "requirements.txt",
    "AGENTS.md",
}


class RepositoryError(RuntimeError):
    pass


@dataclass(slots=True)
class CommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


class ShellExecutor(Protocol):
    def run(self, command: str, cwd: Path, timeout: int, task_id: str | None) -> CommandResult: ...


@dataclass(slots=True)
class LocalSnapshot:
    files: list[str]
    baseline_commit: str | None
    source_status: str


class Repository:
    def __init__(self, path: str | Path, allowed_roots: Iterable[Path]) -> None:
        self.path = Path(path).expanduser().resolve()
        roots = [root.expanduser().resolve() for root in allowed_roots]
        self.allowed_roots = roots
        if not any(self.path == root or self.path.is_relative_to(root) for root in roots):
            raise RepositoryError("Repository is outside AUTOFLOW_ALLOWED_ROOTS")
        if not self.path.is_dir():
            raise RepositoryError(f"Repository does not exist: {self.path}")
        if not (self.path / ".git").exists():
            raise RepositoryError(f"Not a Git repository: {self.path}")

    def ensure_clean(self) -> None:
        result = self.run(["git", "status", "--porcelain"])
        if result.stdout.strip():
            raise RepositoryError(
                "Repository has uncommitted changes. Use a clean worktree to avoid overwriting work."
            )

    def current_branch(self) -> str:
        return self.run(["git", "branch", "--show-current"]).stdout.strip()

    def head_oid(self) -> str | None:
        result = self.run(["git", "rev-parse", "--verify", "HEAD"], check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def checkout_branch(self, branch: str) -> str:
        if not branch:
            return self.current_branch()
        if not self._valid_ref(branch):
            raise RepositoryError("Invalid branch name")
        exists = self.run(["git", "show-ref", "--verify", f"refs/heads/{branch}"], check=False)
        command = (
            ["git", "checkout", branch]
            if exists.returncode == 0
            else ["git", "checkout", "-b", branch]
        )
        self.run(command)
        return branch

    def create_worktree(self, destination: Path, branch: str) -> Repository:
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise RepositoryError(f"Worktree destination already exists: {destination}")
        if not self._valid_ref(branch):
            raise RepositoryError("Invalid branch name")
        has_head = self.run(["git", "rev-parse", "--verify", "HEAD"], check=False).returncode == 0
        if not has_head:
            self.run(["git", "worktree", "add", "--orphan", "-b", branch, str(destination)])
            return Repository(destination, [destination.parent, *self.allowed_roots])
        branch_exists = (
            self.run(
                ["git", "show-ref", "--verify", f"refs/heads/{branch}"], check=False
            ).returncode
            == 0
        )
        command = ["git", "worktree", "add"]
        if not branch_exists:
            command.extend(["-b", branch])
        command.extend([str(destination), branch if branch_exists else "HEAD"])
        self.run(command)
        return Repository(destination, [destination.parent, *self.allowed_roots])

    def copy_local_changes_to(self, destination: Repository, max_bytes: int) -> LocalSnapshot:
        source_status = self.status()
        if not source_status.strip():
            return LocalSnapshot([], None, "")

        has_head = self.run(["git", "rev-parse", "--verify", "HEAD"], check=False).returncode == 0
        tracked: list[str] = []
        patch = b""
        if has_head:
            source_head = self.run(["git", "rev-parse", "HEAD"]).stdout.strip()
            destination_head = destination.run(
                ["git", "rev-parse", "--verify", "HEAD"], check=False
            ).stdout.strip()
            if destination_head != source_head:
                raise RepositoryError(
                    "Local changes can only be included when the target branch starts "
                    "from the source repository HEAD"
                )
            tracked = self._null_paths(
                self.run(["git", "diff", "--name-only", "-z", "HEAD", "--"]).stdout
            )
            completed = subprocess.run(
                [
                    "git",
                    "diff",
                    "--binary",
                    "--full-index",
                    "--ita-visible-in-index",
                    "HEAD",
                    "--",
                ],
                cwd=self.path,
                capture_output=True,
                timeout=120,
            )
            if completed.returncode != 0:
                error = completed.stderr.decode("utf-8", errors="replace")
                raise RepositoryError(f"Unable to capture local tracked changes: {error[-4000:]}")
            patch = completed.stdout

        untracked = self._null_paths(
            self.run(["git", "ls-files", "-z", "--others", "--exclude-standard"]).stdout
        )
        total_bytes = len(patch)
        sources: list[tuple[str, Path]] = []
        for relative in untracked:
            parts = Path(relative).parts
            if any(part in IGNORED_PARTS for part in parts):
                raise RepositoryError(
                    f"Local changes include protected path: {relative}. "
                    "Add generated directories to .gitignore before starting the task."
                )
            source = self.path / relative
            if source.is_symlink():
                raise RepositoryError(
                    f"Untracked symbolic links are not supported in local snapshots: {relative}"
                )
            if not source.is_file():
                raise RepositoryError(f"Unsupported untracked path: {relative}")
            total_bytes += source.stat().st_size
            if total_bytes > max_bytes:
                raise RepositoryError(
                    f"Local changes exceed snapshot size limit ({max_bytes} bytes)"
                )
            sources.append((relative, source))

        if len(patch) > max_bytes:
            raise RepositoryError(f"Local changes exceed snapshot size limit ({max_bytes} bytes)")
        if patch:
            applied = subprocess.run(
                ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
                cwd=destination.path,
                input=patch,
                capture_output=True,
                timeout=120,
            )
            if applied.returncode != 0:
                error = applied.stderr.decode("utf-8", errors="replace")
                raise RepositoryError(f"Unable to apply local tracked changes: {error[-4000:]}")

        for relative, source in sources:
            target = destination._safe_path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        files = list(dict.fromkeys([*tracked, *untracked]))
        if not destination.status().strip():
            return LocalSnapshot(files, None, source_status)
        destination.run(["git", "add", "-A"])
        destination.run(
            [
                "git",
                "-c",
                "user.name=AutoFlow",
                "-c",
                "user.email=autoflow@local",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-m",
                "chore: snapshot local workspace",
            ]
        )
        baseline_commit = destination.run(["git", "rev-parse", "HEAD"]).stdout.strip()
        return LocalSnapshot(files, baseline_commit, source_status)

    def inventory(self, limit: int = 500) -> list[str]:
        result = self.run(["git", "ls-files"])
        return [line for line in result.stdout.splitlines() if line][:limit]

    def recent_history(self, limit: int = 12) -> str:
        return self.run(
            ["git", "log", f"-{limit}", "--pretty=format:%h %s", "--no-decorate"],
            check=False,
        ).stdout

    def search(self, query: str, limit: int = 80) -> str:
        terms = [term for term in query.split() if len(term) >= 3][:8]
        if not terms:
            return ""
        pattern = "|".join(self._escape_regex(term) for term in terms)
        if shutil.which("rg") is None:
            return self._search_with_python(pattern, limit)
        result = self.run(
            [
                "rg",
                "-n",
                "--hidden",
                "--glob",
                "!.git",
                "--glob",
                "!node_modules",
                "--glob",
                "!build",
                "--glob",
                "!target",
                "-i",
                "-e",
                pattern,
                ".",
            ],
            check=False,
        )
        return "\n".join(result.stdout.splitlines()[:limit])

    def _search_with_python(self, pattern: str, limit: int) -> str:
        matcher = re.compile(pattern, re.IGNORECASE)
        listed = self.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            check=False,
        )
        matches: list[str] = []
        for relative in self._null_paths(listed.stdout):
            try:
                path = self._safe_path(relative)
            except RepositoryError:
                continue
            if (
                path.is_symlink()
                or not path.is_file()
                or any(part in IGNORED_PARTS for part in Path(relative).parts)
                or not self._is_text_file(path)
            ):
                continue
            try:
                with path.open(encoding="utf-8", errors="ignore") as source:
                    for line_number, line in enumerate(source, start=1):
                        if not matcher.search(line):
                            continue
                        matches.append(f"{relative}:{line_number}:{line.rstrip()[:500]}")
                        if len(matches) >= limit:
                            return "\n".join(matches)
            except OSError:
                continue
        return "\n".join(matches)

    def collect_context(self, likely_paths: list[str], query: str, max_chars: int) -> str:
        candidates: list[Path] = []
        for item in likely_paths:
            candidate = self._safe_path(item)
            if candidate.is_file():
                candidates.append(candidate)
            elif candidate.is_dir():
                candidates.extend(self._text_files(candidate))

        search_output = self.search(query)
        for line in search_output.splitlines():
            relative = line.split(":", 1)[0].removeprefix("./")
            try:
                candidate = self._safe_path(relative)
            except RepositoryError:
                continue
            if candidate.is_file():
                candidates.append(candidate)

        names = {
            "AGENTS.md",
            "README.md",
            "pyproject.toml",
            "package.json",
            "Cargo.toml",
            "go.mod",
            "CMakeLists.txt",
        }
        for name in names:
            candidate = self.path / name
            if candidate.is_file():
                candidates.append(candidate)

        chunks: list[str] = []
        used: set[Path] = set()
        total = 0
        for candidate in candidates:
            if candidate in used or not self._is_text_file(candidate):
                continue
            used.add(candidate)
            try:
                content = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            remaining = max_chars - total
            if remaining <= 0:
                break
            content = content[:remaining]
            relative_path = candidate.relative_to(self.path)
            chunk = f"\n--- FILE: {relative_path} ---\n{content}"
            chunks.append(chunk)
            total += len(chunk)
        return "".join(chunks)

    def write_changes(self, changes: list[tuple[str, str]]) -> list[str]:
        written: list[str] = []
        for relative, content in changes:
            target = self._safe_path(relative)
            if target == self.path or any(
                part in IGNORED_PARTS for part in target.relative_to(self.path).parts
            ):
                raise RepositoryError(f"Refusing to write protected path: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(str(target.relative_to(self.path)))
        return written

    def diff(self) -> str:
        tracked = self.run(["git", "diff", "--no-ext-diff", "--binary"]).stdout
        untracked = self.run(
            ["git", "ls-files", "--others", "--exclude-standard"]
        ).stdout.splitlines()
        pieces = [tracked]
        for path in untracked:
            result = self.run(["git", "diff", "--no-index", "--", "/dev/null", path], check=False)
            pieces.append(result.stdout)
        return "\n".join(piece for piece in pieces if piece)

    def status(self) -> str:
        return self.run(["git", "status", "--short"]).stdout

    def delivery_files(self) -> list[str]:
        changed = self.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMRTUXB"]
        ).stdout.splitlines()
        untracked = self.run(
            ["git", "ls-files", "--others", "--exclude-standard"]
        ).stdout.splitlines()
        return list(dict.fromkeys(path for path in [*changed, *untracked] if path))

    def read_delivery_file(self, relative: str) -> bytes | None:
        target = self._safe_path(relative)
        if not target.is_file() or ".git" in target.relative_to(self.path).parts:
            return None
        return target.read_bytes()

    def apply_patch(self, patch: str, expected_head: str | None) -> None:
        if self.head_oid() != expected_head:
            raise RepositoryError("Source repository HEAD changed while the workflow was running")
        if not patch.strip():
            return
        completed = subprocess.run(
            ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
            cwd=self.path,
            input=patch,
            text=True,
            capture_output=True,
            timeout=120,
        )
        if completed.returncode != 0:
            detail = (completed.stdout + "\n" + completed.stderr).strip()
            raise RepositoryError(
                "Unable to sync AI changes because the local files changed or conflict: "
                + detail[-4000:]
            )

    def hygiene_findings(self) -> list[str]:
        untracked = self.run(
            ["git", "ls-files", "--others", "--exclude-standard"]
        ).stdout.splitlines()
        generated = [
            path
            for path in untracked
            if any(part in IGNORED_PARTS for part in Path(path).parts)
            or Path(path).suffix in {".pyc", ".pyo", ".class", ".o", ".obj"}
        ]
        if not generated:
            return []
        preview = ", ".join(generated[:12])
        suffix = " ..." if len(generated) > 12 else ""
        return [
            "Generated or cache files are not ignored: "
            + preview
            + suffix
            + ". Add appropriate ignore rules without deleting source files."
        ]

    def commit(self, message: str) -> str:
        if not message.strip():
            raise RepositoryError("Commit message is empty")
        self.run(["git", "add", "-A"])
        self.run(["git", "commit", "-m", message.strip()[:200]])
        return self.run(["git", "rev-parse", "HEAD"]).stdout.strip()

    def remote_url(self, remote: str = "origin") -> str:
        result = self.run(["git", "remote", "get-url", remote], check=False)
        if result.returncode != 0 or not result.stdout.strip():
            raise RepositoryError(f"Git remote is not configured: {remote}")
        return result.stdout.strip()

    def push_with_token(
        self,
        branch: str,
        username: str,
        token: str,
        remote: str = "origin",
    ) -> None:
        if not self._valid_ref(branch):
            raise RepositoryError("Invalid branch name")
        with tempfile.TemporaryDirectory(prefix="autoflow-git-") as directory:
            askpass = Path(directory) / "askpass.sh"
            askpass.write_text(
                """#!/bin/sh
case "$1" in
  *Username*) printf '%s' "$AUTOFLOW_GIT_USERNAME" ;;
  *) printf '%s' "$AUTOFLOW_GIT_TOKEN" ;;
esac
""",
                encoding="utf-8",
            )
            askpass.chmod(0o700)
            environment = {
                **os.environ,
                "GIT_ASKPASS": str(askpass),
                "GIT_TERMINAL_PROMPT": "0",
                "AUTOFLOW_GIT_USERNAME": username,
                "AUTOFLOW_GIT_TOKEN": token,
            }
            completed = subprocess.run(
                ["git", "push", remote, f"HEAD:refs/heads/{branch}"],
                cwd=self.path,
                text=True,
                capture_output=True,
                timeout=300,
                env=environment,
            )
        if completed.returncode != 0:
            detail = (completed.stdout + "\n" + completed.stderr).strip()
            raise RepositoryError(f"Git push failed: {detail[-4000:]}")

    def detect_commands(self) -> tuple[str | None, str | None]:
        if (self.path / "pyproject.toml").exists():
            return None, "pytest"
        if (self.path / "package.json").exists():
            return "npm run build", "npm test -- --runInBand"
        if (self.path / "Cargo.toml").exists():
            return "cargo build", "cargo test"
        if (self.path / "go.mod").exists():
            return "go build ./...", "go test ./..."
        if (self.path / "CMakeLists.txt").exists():
            return (
                "cmake -S . -B build && cmake --build build -j",
                "ctest --test-dir build --output-on-failure",
            )
        if (self.path / "Makefile").exists():
            return "make -j", "make test"
        return None, None

    def run_shell(
        self,
        command: str,
        timeout: int,
        *,
        executor: ShellExecutor | None = None,
        task_id: str | None = None,
    ) -> CommandResult:
        self._validate_shell_command(command)
        if executor is not None:
            return executor.run(command, self.path, timeout, task_id)
        completed = subprocess.run(
            command,
            cwd=self.path,
            shell=True,
            executable="/bin/bash",
            text=True,
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "CI": "1"},
        )
        return CommandResult(command, completed.returncode, completed.stdout, completed.stderr)

    def run(self, command: list[str], *, check: bool = True) -> CommandResult:
        completed = subprocess.run(
            command, cwd=self.path, text=True, capture_output=True, timeout=120
        )
        result = CommandResult(
            shlex.join(command), completed.returncode, completed.stdout, completed.stderr
        )
        if check and completed.returncode != 0:
            raise RepositoryError(f"Command failed: {result.command}\n{result.combined[-4000:]}")
        return result

    def _safe_path(self, relative: str) -> Path:
        if not relative or Path(relative).is_absolute():
            raise RepositoryError(f"Invalid relative path: {relative}")
        resolved = (self.path / relative).resolve()
        if not resolved.is_relative_to(self.path):
            raise RepositoryError(f"Path escapes repository: {relative}")
        return resolved

    def _text_files(self, directory: Path) -> list[Path]:
        files: list[Path] = []
        for path in directory.rglob("*"):
            if len(files) >= 80:
                break
            if (
                path.is_file()
                and not any(part in IGNORED_PARTS for part in path.parts)
                and self._is_text_file(path)
            ):
                files.append(path)
        return files

    @staticmethod
    def _is_text_file(path: Path) -> bool:
        return path.suffix.lower() in TEXT_SUFFIXES or path.name in SPECIAL_TEXT_FILES

    @staticmethod
    def _escape_regex(value: str) -> str:
        return "".join(f"\\{char}" if char in r".^$*+?{}[]\|()" else char for char in value)

    @staticmethod
    def _null_paths(output: str) -> list[str]:
        return [path for path in output.split("\0") if path]

    @staticmethod
    def _valid_ref(value: str) -> bool:
        forbidden = {" ", "..", "~", "^", ":", "?", "*", "[", "\\"}
        return (
            bool(value)
            and not any(item in value for item in forbidden)
            and not value.startswith("-")
        )

    @staticmethod
    def _validate_shell_command(command: str) -> None:
        blocked = [
            "rm -rf",
            "git reset --hard",
            "git clean -fd",
            "git push",
            "sudo ",
            "curl ",
            "wget ",
            "> /dev/",
        ]
        lowered = command.lower()
        if any(token in lowered for token in blocked):
            raise RepositoryError("Command rejected by safety policy")
