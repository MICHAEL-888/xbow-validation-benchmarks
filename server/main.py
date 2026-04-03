from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS_DIR = REPO_ROOT / "benchmarks"
DEFAULT_START_TIMEOUT_SEC = 600
DOCKER_EXECUTABLE_CANDIDATES = ("docker", "docker.exe")


@dataclass(frozen=True)
class BenchmarkRecord:
    benchmark_id: str
    name: str
    description: str
    level: int
    tags: list[str]
    win_condition: str


class BenchmarkRegistry:
    def __init__(self, benchmarks_dir: Path) -> None:
        self._benchmarks_dir = benchmarks_dir
        self._records_by_id: dict[str, BenchmarkRecord] = {}
        self._lookup: dict[str, str] = {}

    def reload(self) -> None:
        records_by_id: dict[str, BenchmarkRecord] = {}
        lookup: dict[str, str] = {}

        if not self._benchmarks_dir.exists():
            self._records_by_id = {}
            self._lookup = {}
            return

        for benchmark_dir in sorted(self._benchmarks_dir.iterdir()):
            if not benchmark_dir.is_dir() or not benchmark_dir.name.startswith("XBEN-"):
                continue

            metadata_path = benchmark_dir / "benchmark.json"
            if not metadata_path.is_file():
                continue

            payload = self._read_json(metadata_path)
            benchmark_id = str(payload.get("id") or benchmark_dir.name)
            name = str(payload.get("name") or benchmark_id)
            description = str(payload.get("description") or "")
            level = int(payload.get("level") or 0)
            tags = payload.get("tags")
            if not isinstance(tags, list):
                tags = []
            tags = [str(tag) for tag in tags]
            win_condition = str(payload.get("win_condition") or "flag")

            record = BenchmarkRecord(
                benchmark_id=benchmark_id,
                name=name,
                description=description,
                level=level,
                tags=tags,
                win_condition=win_condition,
            )
            records_by_id[benchmark_id] = record
            lookup[self._normalize_key(benchmark_id)] = benchmark_id
            lookup[self._normalize_key(name)] = benchmark_id

        self._records_by_id = records_by_id
        self._lookup = lookup

    def list_records(self) -> list[BenchmarkRecord]:
        return [self._records_by_id[key] for key in sorted(self._records_by_id.keys())]

    def resolve(self, benchmark_name: str) -> BenchmarkRecord:
        key = self._normalize_key(benchmark_name)
        benchmark_id = self._lookup.get(key)
        if benchmark_id is None:
            raise KeyError(f"unknown benchmark '{benchmark_name}'")
        return self._records_by_id[benchmark_id]

    def contains_id(self, benchmark_id: str) -> bool:
        return benchmark_id in self._records_by_id

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid json file: {path}") from exc

    @staticmethod
    def _normalize_key(value: str) -> str:
        return value.strip().casefold()


class RuntimeState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active_benchmark_id: Optional[str] = None
        self.started_at: Optional[datetime] = None
        self.health_state: str = "idle"
        self.last_error: Optional[str] = None

    def clear(self) -> None:
        self.active_benchmark_id = None
        self.started_at = None
        self.health_state = "idle"


class ListBenchmarksResponse(BaseModel):
    total: int
    benchmark_names: list[str]


class StartBenchmarkRequest(BaseModel):
    benchmark_name: str = Field(..., min_length=1)
    timeout_sec: int = Field(DEFAULT_START_TIMEOUT_SEC, ge=30, le=3600)


class BenchmarkMetadata(BaseModel):
    benchmark_id: str
    name: str
    description: str
    level: int
    tags: list[str]
    win_condition: str


class StartBenchmarkResponse(BaseModel):
    benchmark: BenchmarkMetadata
    challenge_url: Optional[str]
    run_status: str
    started_at: datetime
    already_running: bool


class VerifyFlagRequest(BaseModel):
    benchmark_name: str = Field(..., min_length=1)
    flag: str = Field(..., min_length=1)


class VerifyFlagResponse(BaseModel):
    benchmark_id: str
    is_correct: bool


registry = BenchmarkRegistry(BENCHMARKS_DIR)
runtime_state = RuntimeState()

app = FastAPI(title="XBOW Benchmark Server", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    registry.reload()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/benchmarks/names", response_model=ListBenchmarksResponse)
def list_benchmark_names() -> ListBenchmarksResponse:
    records = registry.list_records()
    return ListBenchmarksResponse(
        total=len(records),
        benchmark_names=[record.name for record in records],
    )


@app.post("/benchmarks/start", response_model=StartBenchmarkResponse)
def start_benchmark(request: StartBenchmarkRequest) -> StartBenchmarkResponse:
    record = _resolve_or_404(request.benchmark_name)

    with runtime_state.lock:
        _refresh_runtime_state_locked()

        if runtime_state.active_benchmark_id and runtime_state.active_benchmark_id != record.benchmark_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "another benchmark is already running",
                    "active_benchmark_id": runtime_state.active_benchmark_id,
                },
            )

        if runtime_state.active_benchmark_id == record.benchmark_id:
            details = _inspect_runtime(record.benchmark_id)
            return _build_start_response(record, details, already_running=True)

        runtime_state.health_state = "starting"
        runtime_state.last_error = None

        result = _start_benchmark_with_docker_compose(record.benchmark_id, request.timeout_sec)
        if result.returncode != 0:
            runtime_state.health_state = "error"
            runtime_state.last_error = _summarize_command_output(result.stdout, result.stderr)
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "failed to start benchmark",
                    "output": runtime_state.last_error,
                },
            )

        runtime_state.active_benchmark_id = record.benchmark_id
        runtime_state.started_at = datetime.now(timezone.utc)
        details = _inspect_runtime(record.benchmark_id)
        runtime_state.health_state = details["run_status"]

        return _build_start_response(record, details, already_running=False)


@app.post("/benchmarks/verify-flag", response_model=VerifyFlagResponse)
def verify_flag(request: VerifyFlagRequest) -> VerifyFlagResponse:
    record = _resolve_or_404(request.benchmark_name)
    expected_flag = _default_flag_for(record.benchmark_id)
    is_correct = hmac.compare_digest(request.flag.strip(), expected_flag)
    return VerifyFlagResponse(benchmark_id=record.benchmark_id, is_correct=is_correct)


def _resolve_or_404(benchmark_name: str) -> BenchmarkRecord:
    try:
        return registry.resolve(benchmark_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="benchmark not found") from exc


def _refresh_runtime_state_locked() -> None:
    active_id = runtime_state.active_benchmark_id
    if not active_id:
        return

    if not registry.contains_id(active_id):
        runtime_state.clear()
        return

    details = _inspect_runtime(active_id)
    if details["run_status"] == "stopped":
        runtime_state.clear()
        return

    runtime_state.health_state = details["run_status"]


def _build_start_response(
    record: BenchmarkRecord,
    details: dict[str, Any],
    already_running: bool,
) -> StartBenchmarkResponse:
    if runtime_state.started_at is None:
        runtime_state.started_at = datetime.now(timezone.utc)

    benchmark = BenchmarkMetadata(
        benchmark_id=record.benchmark_id,
        name=record.name,
        description=record.description,
        level=record.level,
        tags=record.tags,
        win_condition=record.win_condition,
    )
    return StartBenchmarkResponse(
        benchmark=benchmark,
        challenge_url=details["challenge_url"],
        run_status=details["run_status"],
        started_at=runtime_state.started_at,
        already_running=already_running,
    )


def _inspect_runtime(benchmark_id: str) -> dict[str, Any]:
    benchmark_dir = BENCHMARKS_DIR / benchmark_id
    if not benchmark_dir.is_dir():
        return {"challenge_url": None, "run_status": "stopped"}

    compose_command = _build_docker_compose_command()
    result = _run_command(
        command=compose_command + ["ps", "--format", "json"],
        cwd=benchmark_dir,
        timeout_sec=30,
        action_name="inspect benchmark runtime",
    )
    if result.returncode != 0:
        return {"challenge_url": None, "run_status": "unknown"}

    rows = _parse_compose_rows(result.stdout)
    if not rows:
        return {"challenge_url": None, "run_status": "stopped"}

    run_status = "stopped"
    challenge_url: Optional[str] = None

    for row in rows:
        row_state = str(row.get("State") or "").lower()
        if "healthy" in row_state:
            run_status = "healthy"
        elif "running" in row_state and run_status != "healthy":
            run_status = "running"

        for publisher in _normalize_publishers(row.get("Publishers")):
            if str(publisher.get("Protocol") or "tcp").lower() != "tcp":
                continue
            published_port = publisher.get("PublishedPort")
            if published_port:
                challenge_url = f"http://localhost:{published_port}"
                break

        if challenge_url:
            break

    return {"challenge_url": challenge_url, "run_status": run_status}


def _normalize_publishers(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _parse_compose_rows(raw_output: str) -> list[dict[str, Any]]:
    text = raw_output.strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, dict):
            return [parsed]
        return []
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows


def _start_benchmark_with_docker_compose(
    benchmark_id: str,
    timeout_sec: int,
) -> subprocess.CompletedProcess[str]:
    benchmark_dir = BENCHMARKS_DIR / benchmark_id
    compose_command = _build_docker_compose_command()
    flag = _default_flag_for(benchmark_id)

    build_result = _run_command(
        command=compose_command
        + [
            "build",
            "--build-arg",
            f"FLAG={flag}",
            "--build-arg",
            f"flag={flag}",
        ],
        cwd=benchmark_dir,
        timeout_sec=timeout_sec,
        action_name="build benchmark",
    )
    if build_result.returncode != 0:
        return build_result

    return _run_command(
        command=compose_command + ["up", "--wait"],
        cwd=benchmark_dir,
        timeout_sec=timeout_sec,
        action_name="start benchmark",
    )


def _build_docker_compose_command() -> list[str]:
    docker_executable = _find_executable(DOCKER_EXECUTABLE_CANDIDATES)
    if not docker_executable:
        raise HTTPException(
            status_code=500,
            detail="docker CLI not found in PATH",
        )
    return [docker_executable, "compose"]


def _find_executable(candidates: tuple[str, ...]) -> Optional[str]:
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved

        candidate_path = Path(candidate)
        if candidate_path.is_file():
            return str(candidate_path)

    return None


def _run_command(
    command: list[str],
    cwd: Path,
    timeout_sec: int,
    action_name: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        message = f"{action_name} timed out after {timeout_sec}s"
        raise HTTPException(status_code=500, detail=message) from exc
    except OSError as exc:
        message = f"{action_name} failed: {exc}"
        raise HTTPException(status_code=500, detail=message) from exc


def _summarize_command_output(stdout: str, stderr: str, max_lines: int = 20) -> str:
    lines = (stdout + "\n" + stderr).splitlines()
    tail = lines[-max_lines:]
    return "\n".join(tail).strip()


def _default_flag_for(benchmark_id: str) -> str:
    digest = hashlib.sha256(benchmark_id.upper().encode("utf-8")).hexdigest()
    return f"FLAG{{{digest}}}"
