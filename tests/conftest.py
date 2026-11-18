"""Shared test fixtures."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import Settings, create_app
from app.service import WaferService


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "wafer.db")
    init_db(path)
    return path


@pytest.fixture
def client(db_path: str) -> Iterator[TestClient]:
    app = create_app(Settings(db_path=db_path, scheduler_workers=2))
    with TestClient(app) as test_client:
        yield test_client


def wait_until(client: TestClient, wafer_id: str, predicate, timeout: float = 5.0) -> dict:
    """Poll the status endpoint until ``predicate(body)`` is true."""
    import time

    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/wafers/{wafer_id}/status").json()
        if predicate(body):
            return body
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for state; last status was {body}")


@pytest.fixture
def service(db_path: str) -> WaferService:
    return WaferService(db_path)


def batch(
    *,
    machine: str = "M1",
    origin: tuple[int, int] = (0, 0),
    rotation: int = 0,
    grid: list[list[dict | None]],
    rows: int = 5,
    cols: int = 5,
) -> dict:
    return {
        "wafer_rows": rows,
        "wafer_cols": cols,
        "machine_id": machine,
        "origin_row": origin[0],
        "origin_col": origin[1],
        "rotation_deg": rotation,
        "local_grid": grid,
    }


def cell(verdict: str, retest: int = 0) -> dict:
    return {"verdict": verdict, "retest_no": retest}


def raw_wafer(db_path: str, wafer_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM wafers WHERE wafer_id = ?", (wafer_id,)
        ).fetchone()
    finally:
        conn.close()
