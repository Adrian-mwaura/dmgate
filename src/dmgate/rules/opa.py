"""OPA as a localhost sidecar (DESIGN.md §6.6).

OPA runs as its own process (`opa run --server`) and answers decisions over HTTP on 127.0.0.1. The
soft-rule table and the configuration are pushed into it as data (`PUT /v1/data/...`), so the
lifecycle loop can replace the table without touching the Rego files.

The binary is found at `$DMGATE_OPA`, else `tools/opa/opa.exe` (or `tools/opa/opa`), else `opa` on
the PATH. CLAUDE.md has the download and checksum commands.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
POLICY_DIR = ROOT / "policy" / "rego"
SCHEMA_FILE = ROOT / "schema" / "facts.schema.json"
OPA_VERSION = "1.21.2"  # the version the policies are tested with


class OpaError(RuntimeError):
    pass


def find_opa() -> Path | None:
    configured = os.environ.get("DMGATE_OPA")
    if configured:
        return Path(configured)
    for name in ("opa.exe", "opa"):
        candidate = ROOT / "tools" / "opa" / name
        if candidate.is_file():
            return candidate
    found = shutil.which("opa")
    return Path(found) if found else None


def _require(opa: Path | None) -> Path:
    opa = opa or find_opa()
    if opa is None:
        raise OpaError("OPA binary not found; see CLAUDE.md for the download command")
    return opa


def run_opa(*args: str, opa: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(_require(opa)), *args], capture_output=True, text=True, timeout=120)


def check(opa: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Strict compile plus type check of every policy against the fact schema."""
    return run_opa("check", "--strict", "--schema", str(SCHEMA_FILE), str(POLICY_DIR), opa=opa)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OpaServer:
    """One OPA process serving the policies in `policy_dir`. Use as a context manager."""

    def __init__(self, opa: Path | None = None, policy_dir: Path = POLICY_DIR):
        self.opa = _require(opa)
        self.policy_dir = policy_dir
        self.process: subprocess.Popen[bytes] | None = None
        self.url = ""
        self.port = 0
        self._connection: http.client.HTTPConnection | None = None

    def start(self, timeout: float = 20.0) -> OpaServer:
        port = self.port = _free_port()
        self.url = f"http://127.0.0.1:{port}"
        self.process = subprocess.Popen(
            [str(self.opa), "run", "--server", "--addr", f"127.0.0.1:{port}", "--log-level", "error",
             str(self.policy_dir)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise OpaError(f"OPA exited with code {self.process.returncode}; run `opa check` on the policies")
            try:
                with urllib.request.urlopen(f"{self.url}/health", timeout=1) as response:
                    if response.status == 200:
                        return self
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.1)
        self.close()
        raise OpaError(f"OPA did not become healthy within {timeout} s")

    def _request(self, method: str, path: str, body: Any) -> Any:
        """One request over a kept-alive connection (a new connection per call costs ~10 ms on Windows)."""
        if self.process is None:
            raise OpaError("the OPA server is not running")
        payload = json.dumps(body).encode("utf-8")
        for attempt in (1, 2):
            if self._connection is None:
                self._connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            try:
                self._connection.request(method, f"/v1/data/{path}", payload, {"Content-Type": "application/json"})
                response = self._connection.getresponse()
                raw = response.read()
                break
            except (http.client.HTTPException, ConnectionError):
                self._connection.close()
                self._connection = None
                if attempt == 2:
                    raise
        if response.status >= 400:
            raise OpaError(f"OPA {method} {path}: {response.status} {raw.decode('utf-8', 'replace')}")
        return json.loads(raw) if raw else None

    def put_data(self, path: str, value: Any) -> None:
        self._request("PUT", path, value)

    def query(self, path: str, input_document: Any) -> Any:
        reply = self._request("POST", path, {"input": input_document})
        if "result" not in reply:
            raise OpaError(f"OPA returned no result for {path}: the rule is undefined for this input")
        return reply["result"]

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

    def __enter__(self) -> OpaServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()
