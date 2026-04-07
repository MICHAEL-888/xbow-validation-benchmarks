#!/usr/bin/env python3
"""Build Docker images for all XBEN benchmarks.

Examples:
  python build_all_benchmarks.py
  python build_all_benchmarks.py --jobs 4 --no-cache
  python build_all_benchmarks.py --only XBEN-001-24 XBEN-002-24
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

BENCHMARK_RE = re.compile(r"^XBEN-\d{3}-\d{2}$")
PRINT_LOCK = Lock()


@dataclass
class BuildResult:
    benchmark: str
    return_code: int
    duration_seconds: float


def log(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def discover_benchmarks(benchmarks_dir: Path) -> list[str]:
    benchmark_names: list[str] = []
    for entry in benchmarks_dir.iterdir():
        if not entry.is_dir():
            continue
        if not BENCHMARK_RE.match(entry.name):
            continue
        if not (entry / "benchmark.json").is_file():
            continue
        if not (entry / "docker-compose.yml").is_file():
            continue
        benchmark_names.append(entry.name)
    return sorted(benchmark_names)


def resolve_flag(benchmark_name: str, override_flag: str | None) -> str:
    if override_flag is not None:
        return override_flag

    digest = hashlib.sha256(benchmark_name.upper().encode("utf-8")).hexdigest()
    return f"FLAG{{{digest}}}"


def build_benchmark(
    benchmarks_dir: Path,
    benchmark_name: str,
    *,
    no_cache: bool,
    flag: str | None,
) -> BuildResult:
    build_flag = resolve_flag(benchmark_name, flag)
    benchmark_dir = benchmarks_dir / benchmark_name

    command = [
        "docker",
        "compose",
        "build",
        "--build-arg",
        f"FLAG={build_flag}",
        "--build-arg",
        f"flag={build_flag}",
    ]
    if no_cache:
        command.append("--no-cache")

    start_time = time.perf_counter()
    log(f"\n=== [{benchmark_name}] building ===")
    log(f"[{benchmark_name}] command: {' '.join(command)}")

    process = subprocess.Popen(
        command,
        cwd=benchmark_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )

    assert process.stdout is not None
    for line in process.stdout:
        log(f"[{benchmark_name}] {line.rstrip()}")

    return_code = process.wait()
    duration = time.perf_counter() - start_time

    if return_code == 0:
        log(f"=== [{benchmark_name}] success in {duration:.1f}s ===")
    else:
        log(f"=== [{benchmark_name}] failed (exit {return_code}) in {duration:.1f}s ===")

    return BuildResult(
        benchmark=benchmark_name,
        return_code=return_code,
        duration_seconds=duration,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Docker images for all benchmarks in the repository.",
    )
    parser.add_argument(
        "--benchmarks-dir",
        default="benchmarks",
        help="Directory that contains XBEN-* benchmark folders (default: benchmarks).",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Number of concurrent builds (default: 1).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Pass --no-cache to docker compose build.",
    )
    parser.add_argument(
        "--flag",
        default=None,
        help=(
            "Optional flag value passed as both build args FLAG and flag. "
            "If omitted, a deterministic value is derived from benchmark name."
        ),
    )
    parser.add_argument(
        "--only",
        nargs="+",
        default=None,
        help="Build only these benchmark IDs (e.g. XBEN-001-24 XBEN-002-24).",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop immediately when one build fails (requires --jobs=1).",
    )
    return parser.parse_args()


def print_summary(results: list[BuildResult], elapsed_seconds: float) -> None:
    success = [r for r in results if r.return_code == 0]
    failed = [r for r in results if r.return_code != 0]

    log("\n========== Build Summary ==========")
    log(
        "Total: "
        f"{len(results)} | Succeeded: {len(success)} | Failed: {len(failed)} | "
        f"Elapsed: {elapsed_seconds:.1f}s"
    )

    if failed:
        log("Failed benchmarks:")
        for result in failed:
            log(
                f"  - {result.benchmark}: exit {result.return_code} "
                f"({result.duration_seconds:.1f}s)"
            )


def main() -> int:
    args = parse_args()

    if shutil.which("docker") is None:
        log("error: command 'docker' was not found in PATH")
        return 2

    compose_check = subprocess.run(
        ["docker", "compose", "version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if compose_check.returncode != 0:
        log("error: docker compose plugin is not available")
        return 2

    if args.jobs < 1:
        log("error: --jobs must be >= 1")
        return 2

    if args.fail_fast and args.jobs != 1:
        log("error: --fail-fast can only be used with --jobs=1")
        return 2

    repo_root = Path(__file__).resolve().parent
    benchmarks_dir = (repo_root / args.benchmarks_dir).resolve()

    if not benchmarks_dir.is_dir():
        log(f"error: benchmark directory does not exist: {benchmarks_dir}")
        return 2

    benchmark_names = discover_benchmarks(benchmarks_dir)
    if args.only:
        only_set = {name.strip() for name in args.only}
        missing = sorted(name for name in only_set if name not in benchmark_names)
        if missing:
            log(f"error: benchmark(s) not found: {', '.join(missing)}")
            return 2
        benchmark_names = [name for name in benchmark_names if name in only_set]

    if not benchmark_names:
        log("error: no benchmark directories found")
        return 2

    log(f"Discovered {len(benchmark_names)} benchmark(s).")
    log(f"Using --jobs={args.jobs}")

    started_at = time.perf_counter()
    results: list[BuildResult] = []

    if args.jobs == 1:
        for benchmark_name in benchmark_names:
            result = build_benchmark(
                benchmarks_dir,
                benchmark_name,
                no_cache=args.no_cache,
                flag=args.flag,
            )
            results.append(result)
            if args.fail_fast and result.return_code != 0:
                break
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
            future_map = {
                executor.submit(
                    build_benchmark,
                    benchmarks_dir,
                    benchmark_name,
                    no_cache=args.no_cache,
                    flag=args.flag,
                ): benchmark_name
                for benchmark_name in benchmark_names
            }

            for future in concurrent.futures.as_completed(future_map):
                benchmark_name = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:  # pragma: no cover
                    log(f"=== [{benchmark_name}] failed with internal error: {exc} ===")
                    results.append(
                        BuildResult(
                            benchmark=benchmark_name,
                            return_code=99,
                            duration_seconds=0.0,
                        )
                    )
                else:
                    results.append(result)

    elapsed = time.perf_counter() - started_at
    results.sort(key=lambda item: item.benchmark)
    print_summary(results, elapsed)

    return 0 if all(result.return_code == 0 for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
