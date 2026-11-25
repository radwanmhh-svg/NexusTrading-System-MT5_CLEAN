#!/usr/bin/env python3
"""Local Windows lifecycle helper for the verified NEXUS development stack.

This helper is started by START-NEXUS.ps1 and stopped by STOP-NEXUS.ps1.
It manages only nexus-ml, nexus-api, nexus-risk, and the production dashboard.
Trading mode is forced to PAPER. It does not delete containers, volumes, models,
DuckDB files, or project data.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".nexus-runtime"
LOGS = ROOT / "logs"
STATE_FILE = RUNTIME / "state.json"
PID_FILE = RUNTIME / "manager.pid"
STOP_FILE = RUNTIME / "stop.request"
RUNNER_LOG = LOGS / "nexus-services.log"
DASHBOARD_LOG = LOGS / "nexus-dashboard.log"

SERVICES = [
    ("nexus-data", "http://127.0.0.1:9001/health"),
    ("nexus-features", "http://127.0.0.1:9002/health"),
    ("nexus-api", "http://127.0.0.1:9000/health"),
    ("nexus-ml", "http://127.0.0.1:9003/health"),
    ("nexus-agents", "http://127.0.0.1:9004/health"),
    ("nexus-news", "http://127.0.0.1:9005/health"),
    ("nexus-risk", "http://127.0.0.1:9006/health"),
    ("nexus-executor", "http://127.0.0.1:9007/health"),
    ("nexus-monitor", "http://127.0.0.1:9008/health"),
]
DASHBOARD_URL = "http://127.0.0.1:3100/"


def log(message: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    with (LOGS / "nexus-manager.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def write_state(status: str, **extra: Any) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "manager_pid": os.getpid(),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "trading_mode": "paper",
        **extra,
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def http_ok(url: str, timeout: float = 2.0) -> bool:
    """Return True when the URL answers with a success or redirect status.

    The Next.js dashboard answers 307 on "/", and urllib surfaces that as an
    HTTPError instead of following it, so redirects must count as healthy:
    any answer proves the process is alive.
    """
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "NEXUS-Local-Manager/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 400
    except urllib.error.HTTPError as exc:
        # HTTPError subclasses URLError/OSError: catch it first so a 3xx that
        # urllib did not follow still counts as "the service answered".
        return 200 <= exc.code < 400
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def wait_for_services(timeout: int = 120) -> dict[str, bool]:
    results = {name: False for name, _ in SERVICES}
    deadline = time.time() + timeout
    while time.time() < deadline and not STOP_FILE.exists():
        for name, url in SERVICES:
            if not results[name] and http_ok(url):
                results[name] = True
                log(f"{name} healthy")
        if all(results.values()):
            break
        time.sleep(2)
    return results


def wait_for_url(url: str, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline and not STOP_FILE.exists():
        if http_ok(url):
            return True
        time.sleep(2)
    return False


def refresh_health(results: dict[str, bool]) -> bool:
    """Promote services that were recorded down but answer now.

    Slow services such as nexus-agents can need several minutes beyond the
    initial measurement window, so state.json must not freeze that first
    snapshot. Only False entries are re-probed: upgrades never flap, and a
    dead-service downgrade is nexus-monitor's job, not the lifecycle helper's.
    """
    changed = False
    for name, url in SERVICES:
        if not results[name] and http_ok(url):
            results[name] = True
            log(f"{name} healthy (late recheck)")
            changed = True
    return changed


def launch_runner() -> subprocess.Popen[bytes]:
    """Run run.py under a wrapper that converts Ctrl+Break into KeyboardInterrupt.

    run.py already has a KeyboardInterrupt/finally shutdown path. The wrapper
    makes STOP-NEXUS.ps1 able to request that graceful path on Windows.
    """
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    wrapper = r'''import runpy, signal, sys

def _stop(signum, frame):
    raise KeyboardInterrupt("NEXUS graceful stop requested")

if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _stop)
sys.argv = ["run.py", "--services", "nexus-data,nexus-features,nexus-news,nexus-risk,nexus-ml,nexus-agents,nexus-executor,nexus-monitor,nexus-api", "--no-frontend", "--paper", "--skip-preflight"]
runpy.run_path("run.py", run_name="__main__")
'''
    env = os.environ.copy()
    env["NEXUS_TRADING_MODE"] = "paper"
    env.setdefault("REDIS_URL", "redis://127.0.0.1:6379")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    log_handle = RUNNER_LOG.open("a", encoding="utf-8", buffering=1)
    try:
        proc = subprocess.Popen(
            [str(python), "-u", "-c", wrapper],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
    except Exception:
        log_handle.close()
        raise
    # Keep a reference to the file handle for the lifetime of the process.
    proc._nexus_log_handle = log_handle  # type: ignore[attr-defined]
    log(f"run.py manager started, PID={proc.pid}, services=nexus-data,nexus-features,nexus-news,nexus-risk,nexus-ml,nexus-agents,nexus-executor,nexus-monitor,nexus-api")
    return proc


def launch_dashboard() -> subprocess.Popen[bytes]:
    dashboard_dir = ROOT / "dashboard"
    command = "npm run start -- --hostname 127.0.0.1"
    log_handle = DASHBOARD_LOG.open("a", encoding="utf-8", buffering=1)
    try:
        proc = subprocess.Popen(
            ["cmd.exe", "/d", "/s", "/c", command],
            cwd=str(dashboard_dir),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
    except Exception:
        log_handle.close()
        raise
    proc._nexus_log_handle = log_handle  # type: ignore[attr-defined]
    log(f"Production dashboard process started, PID={proc.pid}, port=3100")
    return proc


def stop_process(proc: subprocess.Popen[bytes] | None, name: str, timeout: int = 15) -> None:
    if proc is None or proc.poll() is not None:
        return
    log(f"Requesting graceful stop: {name} PID={proc.pid}")
    try:
        if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=timeout)
        log(f"Stopped: {name}")
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        log(f"Graceful stop did not finish for {name}: {exc}; using process-tree fallback")
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    finally:
        handle = getattr(proc, "_nexus_log_handle", None)
        if handle:
            try:
                handle.close()
            except Exception:
                pass


def keep_wsl_alive() -> subprocess.Popen[bytes] | None:
    """Keep Ubuntu-24.04 alive while the Windows-managed stack is running."""
    if os.name != "nt":
        return None
    try:
        proc = subprocess.Popen(
            ["wsl.exe", "-d", "Ubuntu-24.04", "--exec", "sleep", "infinity"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log(f"WSL keepalive started, PID={proc.pid}")
        return proc
    except Exception as exc:
        log(f"WARNING: could not start WSL keepalive: {exc}")
        return None


def serve() -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    write_state("starting", services={}, dashboard=False)
    runner: subprocess.Popen[bytes] | None = None
    dashboard: subprocess.Popen[bytes] | None = None
    wsl_keepalive: subprocess.Popen[bytes] | None = None
    try:
        wsl_keepalive = keep_wsl_alive()
        if wsl_keepalive is None:
            raise RuntimeError("Could not keep Ubuntu-24.04 WSL alive; refusing to launch services")
        time.sleep(1)
        if wsl_keepalive.poll() is not None:
            raise RuntimeError(f"WSL keepalive exited early with code {wsl_keepalive.returncode}")
        runner = launch_runner()
        # Launch the dashboard in parallel with the services: slow backends
        # (nexus-agents needs several minutes) must not delay UI availability.
        dashboard = launch_dashboard()
        service_health = wait_for_services(timeout=360)
        if runner.poll() is not None:
            raise RuntimeError(f"NEXUS run.py exited early with code {runner.returncode}; see {RUNNER_LOG}")

        # Final re-probe: a service can come up while the window is closing.
        refresh_health(service_health)
        dashboard_ok = wait_for_url(DASHBOARD_URL, timeout=75)
        all_services_ok = all(service_health.values())
        state = "running" if all_services_ok and dashboard_ok else "degraded"
        urls = {"dashboard": DASHBOARD_URL, **{name: url for name, url in SERVICES}}
        write_state(
            state,
            services=service_health,
            dashboard=dashboard_ok,
            runner_pid=runner.pid,
            dashboard_pid=dashboard.pid,
            urls=urls,
        )
        log(f"Startup finished with state={state}; service_health={service_health}; dashboard={dashboard_ok}")

        # Keep state.json truthful while the stack runs: services slower than
        # the measurement window are promoted here within 30 seconds of
        # becoming healthy, instead of staying frozen at their first snapshot.
        next_recheck = time.time() + 30
        while not STOP_FILE.exists():
            if runner.poll() is not None:
                log(f"ERROR: run.py manager exited with code {runner.returncode}")
                write_state("degraded", services={name: http_ok(url) for name, url in SERVICES}, dashboard=http_ok(DASHBOARD_URL), error=f"run.py exited: {runner.returncode}")
                break
            if dashboard is not None and dashboard.poll() is not None:
                log(f"ERROR: dashboard process exited with code {dashboard.returncode}; see {DASHBOARD_LOG}")
                state = "degraded"
                dashboard_ok = False
                write_state("degraded", services={name: http_ok(url) for name, url in SERVICES}, dashboard=False, error=f"dashboard exited: {dashboard.returncode}")
            if time.time() >= next_recheck:
                next_recheck = time.time() + 30
                changed = refresh_health(service_health)
                if not dashboard_ok and http_ok(DASHBOARD_URL):
                    dashboard_ok = True
                    changed = True
                    log("dashboard healthy (late recheck)")
                if changed:
                    new_state = "running" if all(service_health.values()) and dashboard_ok else "degraded"
                    if new_state != state:
                        state = new_state
                        log(f"Startup state changed to {state} after late recheck")
                    write_state(state, services=dict(service_health), dashboard=dashboard_ok, runner_pid=runner.pid, dashboard_pid=dashboard.pid, urls=urls)
            time.sleep(1)
        return 0
    except Exception as exc:
        log(f"FATAL: {type(exc).__name__}: {exc}")
        write_state("error", error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        write_state("stopping", services={name: http_ok(url) for name, url in SERVICES}, dashboard=http_ok(DASHBOARD_URL))
        stop_process(dashboard, "dashboard", timeout=8)
        stop_process(runner, "NEXUS service runner", timeout=25)
        if wsl_keepalive is not None and wsl_keepalive.poll() is None:
            try:
                wsl_keepalive.terminate()
                wsl_keepalive.wait(timeout=5)
            except Exception:
                try:
                    wsl_keepalive.kill()
                except Exception:
                    pass
        STOP_FILE.unlink(missing_ok=True)
        write_state("stopped", services={name: http_ok(url) for name, url in SERVICES}, dashboard=http_ok(DASHBOARD_URL))
        log("NEXUS manager stopped. Redis container is left untouched by this helper.")
        # Keep the PID file as a harmless audit record; START checks whether
        # the PID is alive before deciding if a manager is already running.


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1].lower() != "serve":
        print("Usage: nexus_stack_manager.py serve", file=sys.stderr)
        return 2
    if os.name != "nt":
        print("This helper package is designed for Windows + WSL2.", file=sys.stderr)
        return 2
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
