"""Part 2: time 5 real-dataset pipeline runs (same callables as weekly CI).

Records ingest / DQ / dbt run / dbt test / total. Prints JSON rows for
docs/reproducibility.md. Does not go through Airflow — the weekly workflow
does not either; Part 1 is the Airflow coverage.

The URL hard-coded in ingest_survey.py currently 404s. This script records
that, then loads the official 2024 public extract from
StackExchange/Survey (Git LFS). ingest_survey.py is not edited.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ingest_survey  # noqa: E402
from dq_checks import run_checks  # noqa: E402
from ingest_survey import SURVEY_ZIP_URL, run as ingest_run  # noqa: E402
from release import (  # noqa: E402
    mark_candidate,
    open_release,
    publish_release,
    record_source_checksum,
)

# Official 2024 public CSV, now hosted as Git LFS on StackExchange/Survey.
# survey.stackoverflow.co "Data & files" for 2024 points here.
ARCHIVE_CSV_URL = (
    "https://media.githubusercontent.com/media/StackExchange/Survey/"
    "main/packages/archive/2024/results.csv"
)
CACHE_CSV = ROOT / "data" / "chaos" / "so_2024_results.csv"
CACHE_ZIP = ROOT / "data" / "chaos" / "so_2024_survey.zip"


def _dbt(subcommand: str, release_id: str) -> None:
    """Run dbt with artifacts off the Compose bind mount.

    `dbt_project/target` is mounted into the Airflow container. On Docker
    Desktop for Windows a file written by Linux dbt can make host dbt fail
    with OSError errno 22 (same failure as overlapping DAG runs). The weekly
    GitHub job is Ubuntu and does not hit this; we still want timings here.
    """
    dbt_dir = ROOT / "dbt_project"
    target_dir = ROOT / "data" / "chaos" / "dbt_target"
    target_dir.mkdir(parents=True, exist_ok=True)
    dbt = Path(sys.executable).resolve().parent / (
        "dbt.exe" if os.name == "nt" else "dbt"
    )
    cmd = [
        str(dbt) if dbt.is_file() else "dbt",
        subcommand,
        "--project-dir",
        str(dbt_dir),
        "--profiles-dir",
        str(dbt_dir),
        "--target-path",
        str(target_dir),
        "--target",
        "prod",
        "--vars",
        '{"release_id": "%s"}' % release_id,
    ]
    subprocess.check_call(cmd, cwd=str(dbt_dir), env=os.environ.copy())


def probe_configured_url() -> dict:
    """Hit ingest_survey.SURVEY_ZIP_URL once. Do not change that constant."""
    try:
        resp = requests.get(SURVEY_ZIP_URL, timeout=30)
        return {
            "url": SURVEY_ZIP_URL,
            "status": resp.status_code,
            "ok": resp.ok,
            "bytes": len(resp.content) if resp.ok else 0,
        }
    except requests.RequestException as exc:
        return {"url": SURVEY_ZIP_URL, "status": None, "ok": False, "error": str(exc)}


def _cim(query: str) -> str:
    """Win32 specs without wmic (removed on recent Windows)."""
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", query],
            text=True,
            timeout=20,
        )
        return " ".join(out.split())
    except Exception as exc:  # noqa: BLE001 — specs are best-effort
        return str(exc)


def machine_specs() -> dict:
    specs = {
        "os": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor(),
        "machine": platform.machine(),
    }
    specs["cpu"] = _cim(
        "(Get-CimInstance Win32_Processor | "
        "Select-Object Name, NumberOfCores, NumberOfLogicalProcessors | Format-List | Out-String)"
    )
    specs["ram_bytes"] = _cim(
        "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"
    )
    specs["computer"] = _cim(
        "(Get-CimInstance Win32_ComputerSystem | "
        "Select-Object Manufacturer, Model | Format-List | Out-String)"
    )
    return specs


def fetch_official_archive_zip() -> dict:
    """Download the GitHub LFS CSV once and wrap it as the public survey ZIP.

    ingest looks for survey_results_public.csv inside a ZIP. The archive
    stores the same table as results.csv.
    """
    CACHE_CSV.parent.mkdir(parents=True, exist_ok=True)
    info = {"url": ARCHIVE_CSV_URL, "cached": CACHE_ZIP.is_file()}
    if CACHE_ZIP.is_file() and CACHE_ZIP.stat().st_size > 1_000_000:
        info["zip_bytes"] = CACHE_ZIP.stat().st_size
        info["download_s"] = 0.0
        info["note"] = "reused cached zip"
        return info
    t0 = time.perf_counter()
    print("Downloading official 2024 CSV from GitHub LFS…", flush=True)
    with requests.get(ARCHIVE_CSV_URL, timeout=300, stream=True) as resp:
        resp.raise_for_status()
        with CACHE_CSV.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
    info["download_s"] = round(time.perf_counter() - t0, 2)
    info["csv_bytes"] = CACHE_CSV.stat().st_size
    # Confirm this is the public extract, not an LFS pointer file.
    with CACHE_CSV.open("r", encoding="utf-8", newline="") as fh:
        header = fh.readline().strip()
    info["csv_header_prefix"] = header[:120]
    if "ResponseId" not in header:
        raise RuntimeError(f"archive CSV missing ResponseId; header={header[:80]!r}")
    with zipfile.ZipFile(CACHE_ZIP, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(CACHE_CSV, arcname="survey_results_public.csv")
    info["zip_bytes"] = CACHE_ZIP.stat().st_size
    # CSV on disk is 159MB; keep the zip, drop the raw CSV to save space.
    CACHE_CSV.unlink(missing_ok=True)
    return info


def install_local_zip_download(zip_path: Path) -> None:
    """In-process stand-in for _download_zip. Does not write ingest_survey.py."""

    def _download_zip() -> bytes:
        ingest_survey.logger.info("MEASURE: reading cached survey ZIP from %s", zip_path)
        return zip_path.read_bytes()

    ingest_survey._download_zip = _download_zip  # type: ignore[method-assign]


def one_run(n: int) -> dict:
    times = {}
    t0 = time.perf_counter()
    rid = open_release()
    t = time.perf_counter()
    ingest_run()
    times["ingest_s"] = round(time.perf_counter() - t, 2)
    t = time.perf_counter()
    record_source_checksum(rid)
    run_checks()
    times["dq_s"] = round(time.perf_counter() - t, 2)
    t = time.perf_counter()
    _dbt("run", rid)
    times["dbt_run_s"] = round(time.perf_counter() - t, 2)
    t = time.perf_counter()
    _dbt("test", rid)
    times["dbt_test_s"] = round(time.perf_counter() - t, 2)
    mark_candidate(rid)
    publish_release(rid)
    times["total_s"] = round(time.perf_counter() - t0, 2)
    times["run"] = n
    times["release_id"] = rid
    print(json.dumps(times), flush=True)
    return times


def main() -> int:
    os.environ.setdefault("SURVEY_DB_HOST", "localhost")
    n = int(os.environ.get("PHASE_D_RUNTIME_RUNS", "5"))
    specs = machine_specs()
    print("SPECS", json.dumps(specs), flush=True)
    configured = probe_configured_url()
    print("CONFIGURED_URL", json.dumps(configured), flush=True)
    archive = fetch_official_archive_zip()
    print("ARCHIVE", json.dumps(archive), flush=True)
    install_local_zip_download(CACHE_ZIP)
    rows = []
    for i in range(1, n + 1):
        print(f"=== real-dataset run {i}/{n} ===", flush=True)
        rows.append(one_run(i))
    keys = ["ingest_s", "dq_s", "dbt_run_s", "dbt_test_s", "total_s"]
    summary = {}
    for k in keys:
        vals = [r[k] for r in rows]
        summary[k] = {
            "runs": vals,
            "median": statistics.median(vals),
            "min": min(vals),
            "max": max(vals),
        }
    print("SUMMARY", json.dumps(summary, indent=2), flush=True)
    out = ROOT / "data" / "chaos" / "runtime_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "configured_url": configured,
        "archive": archive,
        "specs": specs,
        "runs": rows,
        "summary": summary,
        "note": (
            "ingest_survey.SURVEY_ZIP_URL 404s; timings used the official "
            "StackExchange/Survey 2024 Git LFS CSV wrapped as a ZIP. "
            "ingest_survey.py was not modified."
        ),
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
