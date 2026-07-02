from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

from ndx_dca_signal.config import load_config, project_root


BASE_LABEL = "com.sanjeev.ndx-dca-signal"
OLD_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{BASE_LABEL}.plist"
WARM_CACHE_LABEL = f"{BASE_LABEL}.warm-cache"
RUN_DAILY_LABEL = f"{BASE_LABEL}.run-daily"
SETTLE_SIM_TRADES_LABEL = f"{BASE_LABEL}.settle-sim-trades"


def plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def build_plist(label: str, command: str, hour: int, minute: int) -> dict:
    root = project_root()
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    uv_path = shutil.which("uv")
    if uv_path is None:
        raise RuntimeError("uv is not found in PATH")
    return {
        "Label": label,
        "ProgramArguments": [
            uv_path,
            "run",
            "ndx-dca-signal",
            command,
        ],
        "WorkingDirectory": str(root),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(log_dir / f"{command}.out.log"),
        "StandardErrorPath": str(log_dir / f"{command}.err.log"),
    }


def parse_hour_minute(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) < 2:
        raise ValueError(f"invalid launchd time: {value}")
    return int(parts[0]), int(parts[1])


def install_launchd() -> list[Path]:
    config = load_config(None)
    warm_hour, warm_minute = parse_hour_minute(str(config["schedule"].get("warm_cache_time", "14:40:00")))
    run_hour, run_minute = parse_hour_minute(str(config["schedule"].get("run_time", "14:55:00")))
    settle_hour, settle_minute = parse_hour_minute(str(config.get("sim_trading", {}).get("settle_time", "22:30:00")))
    jobs = [
        (WARM_CACHE_LABEL, "warm-cache", warm_hour, warm_minute),
        (RUN_DAILY_LABEL, "run-daily", run_hour, run_minute),
        (SETTLE_SIM_TRADES_LABEL, "settle-sim-trades", settle_hour, settle_minute),
    ]
    paths = []
    OLD_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    if OLD_PLIST_PATH.exists():
        subprocess.run(["launchctl", "unload", str(OLD_PLIST_PATH)], check=False)
        OLD_PLIST_PATH.unlink()
    for label, command, hour, minute in jobs:
        path = plist_path(label)
        with path.open("wb") as f:
            plistlib.dump(build_plist(label, command, hour, minute), f, sort_keys=False)
        subprocess.run(["launchctl", "unload", str(path)], check=False)
        subprocess.run(["launchctl", "load", str(path)], check=True)
        paths.append(path)
    return paths


def uninstall_launchd() -> list[Path]:
    paths = [
        OLD_PLIST_PATH,
        plist_path(WARM_CACHE_LABEL),
        plist_path(RUN_DAILY_LABEL),
        plist_path(SETTLE_SIM_TRADES_LABEL),
    ]
    removed = []
    for path in paths:
        if path.exists():
            subprocess.run(["launchctl", "unload", str(path)], check=False)
            path.unlink()
            removed.append(path)
    return removed
