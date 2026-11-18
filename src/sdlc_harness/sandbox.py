import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@dataclass
class Result:
    returncode: int
    output: str


def process_env() -> dict[str, str]:
    # Never forward the harness's credentials to generated programs.
    return {"PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
            "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}


class Sandbox:
    def __init__(self, mode="local", image="sdlc-harness-sandbox:local", timeout=60):
        if mode not in {"local", "docker"}:
            raise ValueError("Sandbox must be local or docker")
        self.mode, self.image, self.timeout = mode, image, timeout

    def run(self, workspace: Path, args: list[str]) -> Result:
        name = "harness-check-" + uuid4().hex[:12]
        if self.mode == "docker":
            command = ["docker", "run", "--rm", "--name", name, "--network", "none",
                       "--cpus", "1", "--memory", "768m", "--pids-limit", "64",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                       "--user", f"{os.getuid()}:{os.getgid()}",
                       "-v", f"{workspace}:/app"]
            if (workspace / ".git").is_dir():
                command += ["-v", f"{workspace / '.git'}:/app/.git:ro"]
            command += ["-w", "/app", self.image, "python", *args]
        else:
            # Apply limits in a child interpreter; preexec_fn is unsafe in threaded workers.
            wrapper = (
                "import os,resource,sys; "
                "resource.setrlimit(resource.RLIMIT_CPU,(45,45)); "
                "resource.setrlimit(resource.RLIMIT_AS,(805306368,805306368)); "
                "resource.setrlimit(resource.RLIMIT_FSIZE,(4194304,4194304)); "
                "resource.setrlimit(resource.RLIMIT_NOFILE,(128,128)); "
                "os.execv(sys.executable,[sys.executable,*sys.argv[1:]])"
            )
            command = [sys.executable, "-I", "-c", wrapper, *args]
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(command, cwd=workspace, env=process_env(), stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            timed_out = False
            try:
                process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                # Also reap subprocesses left by a command that exited normally.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                if self.mode == "docker":
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
            output.seek(0)
            text = output.read(64000).decode(errors="replace")
        return Result(124 if timed_out else process.returncode, text + ("\nWall-clock timeout" if timed_out else ""))
