import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

from .models import Architecture, Deployment
from .sandbox import process_env
from .workspace import Workspace, safe_path


def process_start(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if stat[0] == "Z" else stat[19]
    except (FileNotFoundError, PermissionError, IndexError):
        return None


class Deployer:
    def __init__(self, timeout=15):
        self.timeout = timeout

    def stop(self, deployment: Deployment):
        if deployment.status == "stopped":
            return
        if deployment.target == "local" and deployment.pid and deployment.process_start:
            if process_start(deployment.pid) == deployment.process_start:
                try:
                    os.killpg(deployment.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 3
                while process_start(deployment.pid) is not None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if process_start(deployment.pid) == deployment.process_start:
                    os.killpg(deployment.pid, signal.SIGKILL)
            try:
                os.waitpid(deployment.pid, os.WNOHANG)
            except ChildProcessError:
                pass
        elif deployment.container and deployment.container.startswith("harness-service-"):
            result = subprocess.run(["docker", "rm", "-f", deployment.container], capture_output=True, text=True, timeout=15)
            if result.returncode and "No such container" not in result.stderr:
                raise RuntimeError(result.stderr[-4000:])
        deployment.status = "stopped"

    def deploy(self, workspace: Workspace, architecture: Architecture, directory: Path,
               config: dict, emit, previous: Deployment | None = None, checkpoint=None) -> Deployment:
        target = config.get("target", "local")
        if target == "docker":
            try:
                available = subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                available = False
            if not available:
                if config.get("fallback") != "local":
                    raise RuntimeError("Docker unavailable and no local fallback configured")
                emit("deploy.fallback", message="Docker unavailable; using configured local fallback")
                target = "local"
        commit = workspace.head()
        release = directory / "releases" / commit
        if not release.exists():
            release.mkdir(parents=True)
            for relative in workspace.files():
                source = safe_path(workspace.root, relative)
                destination = safe_path(release, relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        log_path = directory / f"deploy-{uuid4().hex[:8]}.log"
        record = Deployment(target=target, status="unhealthy", url=f"http://127.0.0.1:{port}")
        process = None
        try:
            if target == "local":
                with log_path.open("wb") as log:
                    process = subprocess.Popen(
                        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
                        cwd=release, env=process_env(), stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                    )
                record.pid, record.process_start = process.pid, process_start(process.pid)
            else:
                record.image = f"harness-{directory.name}:{commit[:12]}"
                record.container = "harness-service-" + uuid4().hex[:12]
                self._docker(["build", "-t", record.image, str(release)], timeout=180)
                self._docker(["run", "-d", "--name", record.container, "--cpus", "1", "--memory", "512m",
                              "--pids-limit", "64", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                              "-p", f"127.0.0.1:{port}:8000", record.image])
            if checkpoint:
                checkpoint(record)
            opener = build_opener(ProxyHandler({}))
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    with opener.open(record.url + "/health", timeout=1) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError, ConnectionError):
                    pass
                if process and process.poll() is not None:
                    raise RuntimeError("Service exited before becoming healthy")
                if time.monotonic() >= deadline:
                    raise RuntimeError("Health check timed out")
                time.sleep(0.1)
            paths = {"/health", "/openapi.json"}
            paths.update(e.path for e in architecture.api if e.method.upper() == "GET" and "{" not in e.path)
            for path in sorted(paths):
                if not path.startswith("/") or path.startswith("//") or "?" in path:
                    raise ValueError(f"Invalid smoke-check path: {path}")
                with opener.open(record.url + path, timeout=3) as response:
                    record.checks.append({"path": path, "status": response.status})
                    if response.status != 200:
                        raise RuntimeError(f"Smoke check failed: {path}")
            record.status = "healthy"
            if previous and previous.status == "healthy":
                self.stop(previous)
        except Exception as error:
            self.stop(record)
            record.status = "unhealthy"
            record.logs = str(error)
            emit("deploy.rollback", message=str(error), previous_url=previous.url if previous else None)
        if target == "local" and log_path.exists():
            record.logs += "\n" + log_path.read_text(errors="replace")[-16000:]
        elif record.container and record.status == "healthy":
            record.logs += self._docker(["logs", record.container])[-16000:]
        emit("deploy.checked", target=target, status=record.status, checks=record.checks)
        if checkpoint:
            checkpoint(record)
        return record

    @staticmethod
    def _docker(args, timeout=30):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr[-8000:])
        return result.stdout
