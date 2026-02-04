from __future__ import annotations

import importlib
import importlib.metadata
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kaira.config import AppConfig

REQUIRED_DISTS: tuple[str, ...] = (
    "duckdb",
    "httpx",
    "hmmlearn",
    "numpy",
    "orjson",
    "pandas",
    "pyarrow",
    "pydantic",
    "scikit-learn",
    "tenacity",
    "typer",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _check_python_version(min_version: tuple[int, int]) -> CheckResult:
    current = sys.version_info
    if current >= min_version:
        return CheckResult(
            name="Python version compatibility",
            ok=True,
            detail=f"{current.major}.{current.minor}.{current.micro}",
        )
    return CheckResult(
        name="Python version compatibility",
        ok=False,
        detail=f"requires >= {min_version[0]}.{min_version[1]}, found {current.major}.{current.minor}.{current.micro}",
    )


def _check_required_packages() -> CheckResult:
    missing: list[str] = []
    versions: list[str] = []
    for dist in REQUIRED_DISTS:
        try:
            versions.append(f"{dist}=={importlib.metadata.version(dist)}")
        except importlib.metadata.PackageNotFoundError:
            missing.append(dist)
    if missing:
        return CheckResult(
            name="Required packages installed",
            ok=False,
            detail=f"missing: {', '.join(missing)}",
        )
    return CheckResult(
        name="Required packages installed",
        ok=True,
        detail=", ".join(versions),
    )


def _check_parquet_writable(data_root: Path) -> CheckResult:
    if importlib.util.find_spec("pyarrow") is None:
        return CheckResult(name="Parquet writable", ok=False, detail="pyarrow not installed")
    pyarrow = importlib.import_module("pyarrow")
    parquet = importlib.import_module("pyarrow.parquet")
    data_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=data_root) as tmp_dir:
            path = Path(tmp_dir) / "doctor_test.parquet"
            table = pyarrow.table({"ok": [1]})
            parquet.write_table(table, path)
        return CheckResult(name="Parquet writable", ok=True, detail=f"wrote under {data_root}")
    except Exception as exc:  # pragma: no cover - env dependent
        return CheckResult(name="Parquet writable", ok=False, detail=str(exc))


def _check_duckdb() -> CheckResult:
    if importlib.util.find_spec("duckdb") is None:
        return CheckResult(name="DuckDB working", ok=False, detail="duckdb not installed")
    duckdb = importlib.import_module("duckdb")
    try:
        conn = duckdb.connect(database=":memory:")
        conn.execute("select 1 as ok").fetchone()
        conn.close()
        return CheckResult(name="DuckDB working", ok=True, detail="in-memory query ok")
    except Exception as exc:  # pragma: no cover - env dependent
        return CheckResult(name="DuckDB working", ok=False, detail=str(exc))


def _check_network_reachable() -> CheckResult:
    if importlib.util.find_spec("httpx") is None:
        return CheckResult(name="Network reachable", ok=False, detail="httpx not installed")
    httpx = importlib.import_module("httpx")
    url = "https://example.com"
    try:
        with httpx.Client(timeout=5.0, follow_redirects=True) as client:
            resp = client.get(url)
        if resp.status_code < 400:
            return CheckResult(name="Network reachable", ok=True, detail=f"{url} -> {resp.status_code}")
        return CheckResult(
            name="Network reachable",
            ok=False,
            detail=f"{url} -> {resp.status_code}",
        )
    except Exception as exc:  # pragma: no cover - env dependent
        return CheckResult(name="Network reachable", ok=False, detail=str(exc))


def _check_nse_endpoint() -> CheckResult:
    if importlib.util.find_spec("httpx") is None:
        return CheckResult(name="NSE endpoint reachable", ok=False, detail="httpx not installed")
    httpx = importlib.import_module("httpx")
    headers = {
        "accept": "application/json,text/plain,*/*",
        "accept-language": "en-US,en;q=0.9",
        "referer": "https://www.nseindia.com/option-chain",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
    }
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            client.get("https://www.nseindia.com/", headers=headers)
            resp = client.get(
                "https://www.nseindia.com/api/option-chain-indices",
                params={"symbol": "NIFTY"},
                headers=headers,
            )
        if resp.status_code == 200:
            return CheckResult(name="NSE endpoint reachable", ok=True, detail="option-chain-indices ok")
        return CheckResult(
            name="NSE endpoint reachable",
            ok=False,
            detail=f"status {resp.status_code}",
        )
    except Exception as exc:  # pragma: no cover - env dependent
        return CheckResult(name="NSE endpoint reachable", ok=False, detail=str(exc))


def run_doctor() -> list[CheckResult]:
    cfg = AppConfig()
    checks = [
        _check_python_version((3, 11)),
        _check_required_packages(),
        _check_parquet_writable(cfg.data.root),
        _check_duckdb(),
        _check_network_reachable(),
        _check_nse_endpoint(),
    ]
    return checks
