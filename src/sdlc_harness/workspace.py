import os
import subprocess
from pathlib import Path, PurePosixPath

from .models import FilePatch


def safe_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (not relative or "\\" in relative or path.is_absolute() or
            any(part in {"..", ".git", ".harness"} for part in path.parts)):
        raise ValueError(f"Unsafe workspace path: {relative}")
    candidate = root / relative
    if candidate == root or not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes workspace: {relative}")
    for part in [candidate, *candidate.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise ValueError(f"Symlinks are not allowed: {relative}")
    return candidate


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def git(self, *args):
        result = subprocess.run(
            ["git", "-c", "user.name=SDLC Harness", "-c", "user.email=harness@localhost",
             "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
            cwd=self.root, capture_output=True, text=True, timeout=30,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.root)},
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    def initialize(self):
        if not (self.root / ".git").exists():
            self.git("init", "-b", "main")
            (self.root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.harness/\n*.pyc\n")
            self.commit("Initialize project workspace")

    def head(self):
        return self.git("rev-parse", "HEAD")

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "--allow-empty", "-m", message)
        return self.head()

    def restore(self, revision):
        self.git("checkout", "-f", "main")
        self.git("reset", "--hard", revision)
        self.git("clean", "-fdx")

    def apply(self, patch: FilePatch, role: str):
        if role not in {"developer", "qa"}:
            raise ValueError(f"Role {role} cannot write files")
        if len(patch.files) > 100 or sum(len(v.encode()) for v in patch.files.values()) > 1_000_000:
            raise ValueError("Patch exceeds POC size budget")
        targets = [(safe_path(self.root, name), content) for name, content in patch.files.items()]
        for target, content in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def context(self, files: list[str], budget=24000):
        output = {}
        for name in sorted(set(files)):
            path = safe_path(self.root, name)
            if not path.is_file():
                output[name] = "[not created]"
            elif path.stat().st_size > budget:
                output[name] = "[omitted: context budget]"
            else:
                content = path.read_text(errors="replace")
                output[name] = content
                budget -= len(content.encode())
        return output

    def files(self, include_untracked=False):
        args = ["--cached", "--others", "--exclude-standard"] if include_untracked else []
        return sorted(set(self.git("ls-files", *args).splitlines()))
