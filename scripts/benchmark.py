"""Stdlib benchmark using an isolated temporary workspace and runtime data."""
from __future__ import annotations
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentboard.store import Store


def usage(pid):
    if sys.platform == "win32":
        from ctypes import wintypes
        class Memory(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in (
                "peak_rss", "rss", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private")]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
                raise ctypes.WinError(ctypes.get_last_error())
            info = Memory()
            info.cb = ctypes.sizeof(info)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(info), info.cb):
                raise ctypes.WinError(ctypes.get_last_error())
            cpu = sum((v.dwHighDateTime << 32) + v.dwLowDateTime for v in values[2:]) / 10**7
            return cpu, info.rss, info.private
        finally:
            kernel.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        cpu = (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK")
        status = Path(f"/proc/{pid}/status").read_text()
        rss = int(re.search(r"VmRSS:\s+(\d+)", status)[1]) * 1024
        try:
            maps = Path(f"/proc/{pid}/smaps_rollup").read_text()
            private = sum(int(v) for v in re.findall(r"Private_(?:Clean|Dirty):\s+(\d+)", maps)) * 1024
        except OSError:
            private = None
        return cpu, rss, private
    raise RuntimeError("Benchmark supports Windows and Linux")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--tasks", type=int, default=100)
    parser.add_argument("--requests", type=int, default=200)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 60 or not 0 <= args.tasks <= 1000 or not 1 <= args.requests <= 10000:
        parser.error("seconds 1..60, tasks 0..1000, requests 1..10000")
    with tempfile.TemporaryDirectory(prefix="agentboard-benchmark-") as directory:
        # Both coordinated resources and runtime documents stay in this temporary tree.
        workspace = Path(directory) / "workspace"
        workspace.mkdir()
        data = Path(directory) / "data"
        store = Store(data, workspace=workspace)
        try:
            for i in range(args.tasks):
                store.create_task({"title": f"Benchmark task {i}", "description": "Local collaboration benchmark.\n" * 8,
                                   "resources": [f"src/module-{i}.py"]})
        finally:
            store.close()
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        process = subprocess.Popen([sys.executable, "-S", "-m", "agentboard", "--port", "0", "--data-dir", str(data),
                                    "--workspace", str(workspace)], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   encoding="utf-8", env=environment,
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        try:
            match = re.search(r"http://127\.0\.0\.1:\d+", process.stdout.readline())
            if not match:
                raise RuntimeError("Benchmark server failed to start")
            base = match[0]
            token = (data / "admin.token").read_text().strip()
            opener = request.build_opener(request.ProxyHandler({}))
            def call(path, payload=None, etag=None):
                headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
                if etag:
                    headers["If-None-Match"] = etag
                req = request.Request(base + path, headers=headers,
                                      data=json.dumps(payload).encode() if payload is not None else None)
                try:
                    response = opener.open(req, timeout=10)
                except error.HTTPError as exc:
                    if exc.code != 304:
                        raise RuntimeError(f"Benchmark HTTP {exc.code}") from None
                    response = exc
                with response:
                    return response.status, response.headers.get("ETag"), response.read()
            for i in range(8):
                call("/api/agents/register", {"name": f"Benchmark agent {i}", "client": "benchmark", "model": "unknown"})
            _, etag, body = call("/api/state")
            cpu_before, _, _ = usage(process.pid)
            before = time.perf_counter()
            time.sleep(args.seconds)
            elapsed = time.perf_counter() - before
            cpu_after, rss, private = usage(process.pid)
            load_before = time.perf_counter()
            hits = 0
            for _ in range(args.requests):
                status, _, payload = call("/api/state", etag=etag)
                hits += status == 304 and not payload
            load_elapsed = time.perf_counter() - load_before
            load_cpu, load_rss, _ = usage(process.pid)
            print(json.dumps({"platform": sys.platform, "python": sys.version.split()[0], "tasks": args.tasks, "agents": 8,
                              "idle_seconds": round(elapsed, 3), "idle_cpu_seconds": round(cpu_after - cpu_before, 6),
                              "idle_one_core_percent": round((cpu_after - cpu_before) / elapsed * 100, 4),
                              "working_set_mib": round(rss / 1048576, 2),
                              "private_mib": round(private / 1048576, 2) if private is not None else None,
                              "state_bytes": len(body), "conditional_requests": args.requests, "not_modified": hits,
                              "request_wall_seconds": round(load_elapsed, 3), "request_cpu_seconds": round(load_cpu - cpu_after, 6),
                              "after_requests_working_set_mib": round(load_rss / 1048576, 2)}, indent=2))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    main()
