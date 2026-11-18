"""API-level behaviour: merging, conflicts, pending state, concurrency."""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from app.mapping import LocalMeasurement, map_local_to_full
from app.main import Settings, create_app
from app.scheduler import RecomputeScheduler
from app.service import WaferService
from tests.conftest import batch, cell, raw_wafer, wait_until


def ready(client: TestClient, wafer_id: str) -> dict:
    return wait_until(
        client, wafer_id, lambda s: s["status"] in {"ready", "empty"}
    )


def test_highest_retest_wins_and_untested_cells_are_blank(client: TestClient) -> None:
    first = client.post(
        "/wafers/W/batches",
        json=batch(
            machine="M1",
            origin=(0, 0),
            grid=[[cell("PASS", 0), cell("FAIL", 1)], [None, cell("PASS", 0)]],
        ),
    )
    assert first.status_code == 202
    assert first.json()["status"] == "pending_update"
    ready(client, "W")

    second = client.post(
        "/wafers/W/batches",
        json=batch(
            machine="M2",
            origin=(0, 1),
            grid=[[cell("PASS", 2), cell("PASS", 0)]],
        ),
    )
    assert second.status_code == 202
    ready(client, "W")

    body = client.get("/wafers/W/map", params={"dense": True}).json()
    assert body["status"] == "ready"
    assert body["pending_update"] is False
    assert body["accepted_seq"] == body["map_seq"] == 2
    assert body["tested_cells"] == 4
    assert body["grid"] == [
        ["PASS", "PASS", "PASS", None, None],
        [None, "PASS", None, None, None],
        [None, None, None, None, None],
        [None, None, None, None, None],
        [None, None, None, None, None],
    ]
    by_position = {(c["row"], c["col"]): c for c in body["cells"]}
    # Position (0,1) got retest 1 from M1 and retest 2 from M2: retest 2 wins.
    assert by_position[(0, 1)]["verdict"] == "PASS"
    assert by_position[(0, 1)]["retest_no"] == 2
    assert by_position[(0, 1)]["source"]["machine_id"] == "M2"
    # Untested positions are simply absent from the sparse list.
    assert (2, 2) not in by_position


def test_source_points_back_to_local_cell_and_batch(client: TestClient) -> None:
    upload = client.post(
        "/wafers/W/batches",
        json=batch(machine="M1", origin=(2, 3), rotation=90, grid=[[cell("FAIL", 3)]]),
    ).json()
    ready(client, "W")
    cell_payload = client.get("/wafers/W/map/cells/2/3").json()
    assert cell_payload["tested"] is True
    assert cell_payload["cell"]["source"] == {
        "machine_id": "M1",
        "batch_id": upload["batch_id"],
        "batch_seq": 1,
        "local_row": 0,
        "local_col": 0,
    }
    assert client.get("/wafers/W/map/cells/0/0").json()["tested"] is False


def test_conflict_rejects_batch_and_preserves_previous_map(client: TestClient) -> None:
    client.post(
        "/wafers/W/batches",
        json=batch(machine="M1", grid=[[cell("PASS", 5)]]),
    )
    ready(client, "W")
    before = client.get("/wafers/W/map").json()

    response = client.post(
        "/wafers/W/batches",
        json=batch(machine="M2", grid=[[cell("FAIL", 5)]]),
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "VERDICT_CONFLICT"
    assert error["details"]["conflicts"][0]["stored_verdict"] == "PASS"
    assert error["details"]["conflicts"][0]["incoming_verdict"] == "FAIL"

    after = client.get("/wafers/W/map").json()
    assert after["map_seq"] == before["map_seq"]
    assert after["accepted_seq"] == 1  # rejection did not advance acceptance
    assert after["cells"] == before["cells"]
    assert after["status"] == "ready"
    assert client.get("/wafers/W/batches").json()["batches"].__len__() == 1


def test_conflict_detected_after_rotation_into_same_position(client: TestClient) -> None:
    # Local (0,0) of the second machine lands exactly on the stored (2,2) cell.
    client.post(
        "/wafers/W/batches",
        json=batch(machine="M1", origin=(2, 2), grid=[[cell("PASS", 0)]]),
    )
    ready(client, "W")
    response = client.post(
        "/wafers/W/batches",
        json=batch(
            machine="M2",
            origin=(1, 1),
            rotation=0,
            grid=[[cell("PASS", 0), cell("PASS", 0)], [cell("PASS", 0), cell("FAIL", 0)]],
        ),
    )
    assert response.status_code == 409
    assert response.json()["error"]["details"]["conflicts"][0] == {
        "full_row": 2,
        "full_col": 2,
        "retest_no": 0,
        "stored_verdict": "PASS",
        "incoming_verdict": "FAIL",
    }


def test_identical_reading_is_accepted_and_takes_newest_source(client: TestClient) -> None:
    client.post("/wafers/W/batches", json=batch(machine="M1", grid=[[cell("PASS", 1)]]))
    ready(client, "W")
    second = client.post(
        "/wafers/W/batches", json=batch(machine="M2", grid=[[cell("PASS", 1)]])
    )
    assert second.status_code == 202
    ready(client, "W")
    body = client.get("/wafers/W/map").json()
    assert len(body["cells"]) == 1
    assert body["cells"][0]["source"]["machine_id"] == "M2"


def test_rotation_is_bijective_so_one_upload_cannot_collide(
    client: TestClient,
) -> None:
    """Distinct local cells always map to distinct wafer positions."""
    mapped = map_local_to_full(
        [
            LocalMeasurement(0, 0, "PASS", 0),
            LocalMeasurement(0, 1, "FAIL", 0),
            LocalMeasurement(1, 0, "PASS", 1),
            LocalMeasurement(1, 1, "FAIL", 1),
        ],
        origin_row=2,
        origin_col=2,
        rotation_deg=90,
    )
    assert len({(m.full_row, m.full_col) for m in mapped}) == 4


def test_out_of_bounds_rotation_is_rejected_and_map_preserved(client: TestClient) -> None:
    client.post("/wafers/W/batches", json=batch(grid=[[cell("PASS", 0)]]))
    ready(client, "W")
    before = client.get("/wafers/W/map").json()

    response = client.post(
        "/wafers/W/batches",
        json=batch(origin=(0, 0), rotation=270, grid=[[cell("PASS", 0), cell("FAIL", 0)]]),
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "CELL_OUT_OF_BOUNDS"
    assert error["details"]["offending_count"] == 1
    after = client.get("/wafers/W/map").json()
    assert after["accepted_seq"] == before["accepted_seq"] == 1
    assert after["cells"] == before["cells"]


def test_dimension_mismatch_rejected(client: TestClient) -> None:
    client.post("/wafers/W/batches", json=batch(grid=[[cell("PASS", 0)]]))
    response = client.post(
        "/wafers/W/batches", json=batch(grid=[[cell("PASS", 0)]], rows=4, cols=4)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WAFER_DIMENSION_MISMATCH"


def test_unknown_wafer_returns_404(client: TestClient) -> None:
    assert client.get("/wafers/nope/map").status_code == 404
    assert client.get("/wafers/nope/map/cells/0/0").status_code == 404
    assert client.post("/wafers/nope/recompute").status_code == 404


def test_validation_errors_are_wrapped(client: TestClient) -> None:
    response = client.post(
        "/wafers/W/batches",
        json={"wafer_rows": 0, "wafer_cols": 5, "machine_id": "M", "origin_row": 0,
              "origin_col": 0, "rotation_deg": 45, "local_grid": []},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------- pending
def test_new_acceptance_marks_old_map_pending_until_published(db_path: str) -> None:
    """Drive the service directly so the scheduler cannot race the assertion."""
    service = WaferService(db_path)
    from app.schemas import BatchRequest

    service.accept_batch(
        "W", BatchRequest(**batch(machine="M1", grid=[[cell("PASS", 0)]]))
    )
    service.recompute("W")
    first = service.get_map("W")
    assert first["status"] == "ready"

    service.accept_batch(
        "W", BatchRequest(**batch(machine="M2", grid=[[cell("PASS", 1)]]))
    )
    pending = service.get_map("W")
    assert pending["status"] == "pending_update"
    assert pending["pending_update"] is True
    assert pending["accepted_seq"] == 2
    assert pending["map_seq"] == 1
    # The old map is still fully readable while pending.
    assert pending["cells"] == first["cells"]

    service.recompute("W")
    final = service.get_map("W")
    assert final["status"] == "ready"
    assert final["map_seq"] == 2
    assert final["cells"][0]["retest_no"] == 1


def test_stale_snapshot_publication_is_refused(db_path: str) -> None:
    """A snapshot built before a newer acceptance must not be published."""
    from app.schemas import BatchRequest

    service = WaferService(db_path)
    service.accept_batch(
        "W", BatchRequest(**batch(machine="M1", grid=[[cell("PASS", 0)]]))
    )
    old_snapshot = service._build_snapshot("W")
    assert old_snapshot.seq == 1

    service.accept_batch(
        "W", BatchRequest(**batch(machine="M2", grid=[[cell("FAIL", 1)]]))
    )

    assert service._publish(old_snapshot) is False
    state = service.get_wafer_state("W")
    assert state["accepted_seq"] == 2
    assert state["committed_seq"] == 0  # nothing stale was written
    pending = service.get_map("W")
    assert pending["status"] == "pending_update"
    assert pending["cells"] == []


def test_inflight_recompute_catches_up_after_newer_acceptance(db_path: str) -> None:
    """A slow recompute that loses the race re-snapshots and publishes fresh data."""
    from app.schemas import BatchRequest

    service = WaferService(db_path, recompute_delay_seconds=0.25)
    service.accept_batch(
        "W", BatchRequest(**batch(machine="M1", grid=[[cell("PASS", 0)]]))
    )

    failures: list[BaseException] = []

    def slow_recompute() -> None:
        try:
            service.recompute("W")
        except BaseException as exc:  # pragma: no cover
            failures.append(exc)

    worker = threading.Thread(target=slow_recompute)
    worker.start()
    time.sleep(0.1)  # let the first (soon stale) snapshot be taken
    service.accept_batch(
        "W", BatchRequest(**batch(machine="M2", grid=[[cell("FAIL", 1)]]))
    )
    # While the worker is busy, a query still serves the pending state.
    assert service.get_map("W")["status"] == "pending_update"
    worker.join()
    assert not failures

    final = service.get_map("W")
    assert final["map_seq"] == 2
    assert final["cells"][0]["verdict"] == "FAIL"
    assert final["cells"][0]["retest_no"] == 1
    assert final["cells"][0]["source"]["batch_seq"] == 2


def test_two_machines_uploads_are_accepted_while_recompute_runs(db_path: str) -> None:
    service = WaferService(db_path, recompute_delay_seconds=0.2)
    scheduler = RecomputeScheduler(service, workers=2)
    from app.schemas import BatchRequest

    try:
        scheduler.submit("W")  # no-op on unknown wafer; quiet failure recorded? no
    except Exception:  # pragma: no cover
        pass

    service.accept_batch(
        "W", BatchRequest(**batch(machine="M1", grid=[[cell("PASS", 0)]]))
    )
    scheduler.submit("W")
    time.sleep(0.05)
    # M2 upload is accepted while M1's recompute is still in flight.
    started = time.monotonic()
    service.accept_batch(
        "W", BatchRequest(**batch(machine="M2", grid=[[cell("PASS", 1)]]))
    )
    assert time.monotonic() - started < 0.15
    scheduler.submit("W")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = service.get_wafer_state("W")
        if state["committed_seq"] == state["accepted_seq"] == 2:
            break
        time.sleep(0.02)
    else:  # pragma: no cover
        raise AssertionError("recompute never caught up")
    scheduler.shutdown()
    final = service.get_map("W")
    assert final["status"] == "ready"
    assert final["cells"][0]["source"]["machine_id"] == "M2"


# -------------------------------------------------------------------- recovery
def test_pending_state_and_map_survive_restart(db_path: str) -> None:
    from app.schemas import BatchRequest

    first = WaferService(db_path)
    first.accept_batch(
        "W", BatchRequest(**batch(machine="M1", grid=[[cell("PASS", 0)]]))
    )
    first.recompute("W")
    first.accept_batch(
        "W", BatchRequest(**batch(machine="M2", grid=[[cell("FAIL", 1)]]))
    )

    # "Restart": brand new service instance over the same database.
    second = WaferService(db_path)
    pending = second.get_map("W")
    assert pending["status"] == "pending_update"
    assert pending["accepted_seq"] == 2 and pending["map_seq"] == 1
    assert len(pending["cells"]) == 1

    recovered = second.recompute("W")
    assert recovered["status"] == "ready"
    assert recovered["map_seq"] == 2
    final = second.get_map("W")
    assert final["cells"][0]["verdict"] == "FAIL"
    assert final["cells"][0]["source"]["batch_seq"] == 2
    assert final["accepted_seq"] == final["map_seq"] == 2


def test_failed_recompute_is_reported_and_recoverable(db_path: str, monkeypatch) -> None:
    from app.schemas import BatchRequest

    service = WaferService(db_path)
    service.accept_batch(
        "W", BatchRequest(**batch(machine="M1", grid=[[cell("PASS", 0)]]))
    )

    def boom(_snapshot):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(service, "_publish", boom)
    scheduler = RecomputeScheduler(service, workers=1)
    scheduler.submit("W")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if service.get_map("W")["recompute_failed"]:
            break
        time.sleep(0.02)
    else:  # pragma: no cover
        raise AssertionError("failure was not recorded")
    scheduler.shutdown()

    failed = service.get_map("W")
    assert failed["status"] == "pending_update"
    assert failed["recompute_failed"] is True
    assert failed["last_recompute_error"]["seq"] == 1
    assert failed["cells"] == []

    # Restart and retry with a healthy publisher.
    monkeypatch.undo()
    revived = WaferService(db_path)
    # Pending work is discoverable purely from the database.
    assert revived.pending_wafer_ids() == ["W"]
    revived.recompute("W")
    healed = revived.get_map("W")
    assert healed["status"] == "ready"
    assert healed["last_recompute_error"] is None
    assert len(healed["cells"]) == 1


def test_startup_recovers_interrupted_work(db_path: str) -> None:
    from app.schemas import BatchRequest

    service = WaferService(db_path)
    service.accept_batch(
        "W", BatchRequest(**batch(machine="M1", grid=[[cell("PASS", 0)]]))
    )
    assert raw_wafer(db_path, "W")["committed_seq"] == 0

    app = create_app(Settings(db_path=db_path, scheduler_workers=1))
    with TestClient(app):
        pass  # lifespan startup recovers pending work, shutdown waits for it
    state = raw_wafer(db_path, "W")
    assert state["committed_seq"] == state["accepted_seq"] == 1


def test_recompute_endpoint_is_idempotent(client: TestClient) -> None:
    client.post("/wafers/W/batches", json=batch(grid=[[cell("PASS", 0)]]))
    ready(client, "W")
    response = client.post("/wafers/W/recompute")
    assert response.status_code == 202
    body = response.json()
    assert body["accepted_seq"] == body["map_seq"] == 1
    assert body["status"] == "ready"


# ------------------------------------------------------------ multi-machine
def test_four_machines_with_different_rotations_tile_one_wafer(
    client: TestClient,
) -> None:
    """Each machine contributes a quadrant, rotated around its own origin."""
    quadrants = [
        ("M0", (0, 0), 0, (0, 0)),
        ("M1", (0, 4), 90, (0, 3)),
        ("M2", (4, 4), 180, (3, 3)),
        ("M3", (4, 0), 270, (3, 0)),
    ]
    grid = [[cell("PASS", 0), cell("FAIL", 1)], [cell("PASS", 2), None]]
    for machine, origin, rotation, _ in quadrants:
        response = client.post(
            "/wafers/W/batches",
            json=batch(machine=machine, origin=origin, rotation=rotation, grid=grid),
        )
        assert response.status_code == 202, response.text
        ready(client, "W")

    body = client.get("/wafers/W/map").json()
    assert body["status"] == "ready"
    assert body["tested_cells"] == 12
    machines = {c["source"]["machine_id"] for c in body["cells"]}
    assert machines == {"M0", "M1", "M2", "M3"}
    # Every source must identify the exact local cell that produced it.
    for c in body["cells"]:
        r0 = c["row"]
        c0 = c["col"]
        assert 0 <= c["source"]["local_row"] < 2
        assert 0 <= c["source"]["local_col"] < 2
        assert (r0, c0) not in {(None, None)}

    # Rotation 90 from origin (0,4): offset (dr,dc) -> (dc,-dr), so
    # local (0,1) -> (1,4) and local (1,0) -> (0,3).
    by_pos = {(c["row"], c["col"]): c for c in body["cells"]}
    assert by_pos[(1, 4)]["verdict"] == "FAIL"
    assert by_pos[(1, 4)]["source"]["local_row"] == 0
    assert by_pos[(1, 4)]["source"]["local_col"] == 1
    assert by_pos[(0, 3)]["verdict"] == "PASS"
    assert by_pos[(0, 3)]["source"]["machine_id"] == "M1"
    # The untested local (1,1) cell maps to (1,3) and stays blank.
    assert (1, 3) not in by_pos
    assert by_pos[(0, 3)]["retest_no"] == 2


def test_pending_state_is_queryable_while_slow_recompute_runs(tmp_path) -> None:
    db = str(tmp_path / "slow.db")
    app = create_app(Settings(db_path=db, scheduler_workers=1))
    with TestClient(app) as client:
        client.post("/wafers/W/batches", json=batch(machine="M1", grid=[[cell("PASS", 0)]]))
        ready(client, "W")

        # Directly configure the running service to be slow for the next pass.
        client.app.state.service.recompute_delay_seconds = 0.4
        started = time.monotonic()
        upload = client.post(
            "/wafers/W/batches",
            json=batch(machine="M2", origin=(1, 1), grid=[[cell("PASS", 1)]]),
        )
        elapsed = time.monotonic() - started
        assert upload.status_code == 202
        assert elapsed < 0.3, "upload must not wait for the recompute"

        pending = client.get("/wafers/W/status").json()
        assert pending["status"] == "pending_update"
        assert pending["accepted_seq"] == 2
        assert pending["map_seq"] == 1

        waited = wait_until(client, "W", lambda s: s["status"] == "ready")
        assert waited["map_seq"] == 2


def test_restart_serves_map_and_sources_from_one_snapshot(db_path: str) -> None:
    from app.schemas import BatchRequest

    writer = WaferService(db_path)
    writer.accept_batch(
        "W", BatchRequest(**batch(machine="M1", grid=[[cell("PASS", 0), cell("FAIL", 1)]]))
    )
    writer.recompute("W")
    writer.accept_batch(
        "W", BatchRequest(**batch(machine="M2", origin=(1, 0), grid=[[cell("PASS", 2)]]))
    )
    # Crash before the second recompute completes: only acceptance persisted.
    reader = WaferService(db_path)
    pending = reader.get_map("W")
    assert pending["map_seq"] == 1 and pending["accepted_seq"] == 2

    batches = {b.batch_id: b for b in reader.list_batches("W")}
    # The served map is still entirely from batch 1, and every source resolves.
    for c in pending["cells"]:
        source = c["source"]
        assert source["batch_id"] in batches
        assert batches[source["batch_id"]].seq == source["batch_seq"] == 1
        assert source["batch_seq"] <= pending["map_seq"]

    reader.recompute("W")
    healed = reader.get_map("W")
    assert healed["map_seq"] == 2
    for c in healed["cells"]:
        assert c["source"]["batch_seq"] <= healed["map_seq"]
    sources = {c["source"]["machine_id"] for c in healed["cells"]}
    assert sources == {"M1", "M2"}


def test_invalid_wafer_id_is_rejected_by_path_validation(client: TestClient) -> None:
    response = client.get("/wafers/bad%20id/map")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------- edge cases
def test_rejected_first_upload_does_not_create_wafer(client: TestClient) -> None:
    # Out-of-bounds rotation is rejected before anything is persisted.
    response = client.post(
        "/wafers/NEW/batches",
        json=batch(origin=(0, 0), rotation=270, grid=[[cell("PASS", 0), cell("FAIL", 0)]]),
    )
    assert response.status_code == 422
    assert client.get("/wafers/NEW/map").status_code == 404
    assert client.get("/wafers").json()["wafers"] == []


def test_rejection_leaves_pending_state_untouched(client: TestClient) -> None:
    client.post("/wafers/W/batches", json=batch(machine="M1", grid=[[cell("PASS", 0)]]))
    ready(client, "W")
    # Force a pending window by pointing the running service at a slow build.
    client.app.state.service.recompute_delay_seconds = 0.3
    client.post("/wafers/W/batches", json=batch(machine="M2", grid=[[cell("PASS", 1)]]))
    before = client.get("/wafers/W/status").json()
    assert before["accepted_seq"] == 2 and before["map_seq"] == 1

    conflict = client.post(
        "/wafers/W/batches", json=batch(machine="M3", grid=[[cell("FAIL", 0)]])
    )
    assert conflict.status_code == 409
    after = client.get("/wafers/W/status").json()
    assert after["accepted_seq"] == 2  # rejection consumed no sequence number
    assert client.get("/wafers/W/batches").json()["batches"].__len__() == 2
    # The in-flight rebuild still finishes from the accepted data only.
    wait_until(client, "W", lambda s: s["status"] == "ready")
    cells = client.get("/wafers/W/map").json()["cells"]
    assert {(c["row"], c["col"]): c["verdict"] for c in cells}[(0, 0)] == "PASS"


def test_retest_numbers_are_compared_before_verdicts(client: TestClient) -> None:
    """A lower retest with a different verdict is not a conflict."""
    client.post("/wafers/W/batches", json=batch(machine="M1", grid=[[cell("PASS", 3)]]))
    ready(client, "W")
    response = client.post(
        "/wafers/W/batches", json=batch(machine="M2", grid=[[cell("FAIL", 2)]])
    )
    assert response.status_code == 202
    ready(client, "W")
    cells = client.get("/wafers/W/map").json()["cells"]
    assert len(cells) == 1
    assert cells[0]["verdict"] == "PASS"
    assert cells[0]["retest_no"] == 3
    assert cells[0]["source"]["machine_id"] == "M1"


def test_unmeasured_cells_do_not_clear_existing_results(client: TestClient) -> None:
    client.post(
        "/wafers/W/batches",
        json=batch(machine="M1", grid=[[cell("PASS", 0), cell("FAIL", 0)]]),
    )
    ready(client, "W")
    response = client.post(
        "/wafers/W/batches",
        json=batch(machine="M2", grid=[[None, None]]),
    )
    # No measured cells at all: rejected as a meaningless payload.
    assert response.status_code == 422
    response = client.post(
        "/wafers/W/batches",
        json=batch(machine="M2", grid=[[None, cell("PASS", 1)]]),
    )
    assert response.status_code == 202
    ready(client, "W")
    cells = {(c["row"], c["col"]): c for c in client.get("/wafers/W/map").json()["cells"]}
    assert cells[(0, 0)]["verdict"] == "PASS"
    assert cells[(0, 0)]["retest_no"] == 0  # untouched by the null cell
    assert cells[(0, 1)]["retest_no"] == 1


def test_conflicts_are_truncated_with_a_total(client: TestClient) -> None:
    grid = [[cell("PASS", 0) for _ in range(8)] for _ in range(8)]
    client.post("/wafers/W/batches", json=batch(rows=8, cols=8, grid=grid))
    ready(client, "W")
    reversed_grid = [[cell("FAIL", 0) for _ in range(8)] for _ in range(8)]
    response = client.post(
        "/wafers/W/batches", json=batch(rows=8, cols=8, grid=reversed_grid)
    )
    assert response.status_code == 409
    conflicts = response.json()["error"]["details"]["conflicts"]
    assert len(conflicts) == 51  # 50 examples + truncation marker
    assert conflicts[-1] == {"truncated": True, "total_conflicts": 64, "returned": 50}


def test_dense_grid_size_limit(client: TestClient) -> None:
    # A 501x500 wafer is 250500 cells; accepted for a single sparse upload.
    response = client.post(
        "/wafers/BIG/batches",
        json=batch(rows=501, cols=500, grid=[[cell("PASS", 0)]]),
    )
    assert response.status_code == 202
    ready(client, "BIG")
    assert client.get("/wafers/BIG/map").status_code == 200
    too_big = client.get("/wafers/BIG/map", params={"dense": True})
    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "DENSE_MAP_TOO_LARGE"


def test_map_query_is_not_blocked_by_a_slow_recompute(tmp_path) -> None:
    db = str(tmp_path / "block.db")
    app = create_app(Settings(db_path=db, scheduler_workers=1))
    with TestClient(app) as client:
        client.post("/wafers/W/batches", json=batch(machine="M1", grid=[[cell("PASS", 0)]]))
        ready(client, "W")
        client.app.state.service.recompute_delay_seconds = 0.6
        client.post("/wafers/W/batches", json=batch(machine="M2", grid=[[cell("PASS", 1)]]))
        started = time.monotonic()
        body = client.get("/wafers/W/map").json()
        assert time.monotonic() - started < 0.3
        assert body["status"] == "pending_update"
        assert len(body["cells"]) == 1  # previous snapshot is still served
        wait_until(client, "W", lambda s: s["status"] == "ready")


def test_every_source_reconstructs_to_its_own_position(client: TestClient) -> None:
    """Provenance must be exact: local coords + batch transform = map position."""
    from app.mapping import rotate_offset

    uploads = [
        ("M0", (0, 0), 0),
        ("M1", (0, 5), 90),
        ("M2", (5, 5), 180),
        ("M3", (5, 0), 270),
    ]
    grid = [
        [cell("PASS", 0), cell("FAIL", 0), cell("PASS", 1)],
        [cell("FAIL", 2), None, cell("PASS", 0)],
        [cell("PASS", 0), cell("FAIL", 1), cell("PASS", 0)],
    ]
    for machine, origin, rotation in uploads:
        assert client.post(
            "/wafers/W/batches",
            json=batch(machine=machine, origin=origin, rotation=rotation, grid=grid, rows=6, cols=6),
        ).status_code == 202
        ready(client, "W")

    batches = {b["batch_id"]: b for b in client.get("/wafers/W/batches").json()["batches"]}
    body = client.get("/wafers/W/map").json()
    assert body["status"] == "ready"
    for c in body["cells"]:
        src = c["source"]
        b = batches[src["batch_id"]]
        assert b["machine_id"] == src["machine_id"]
        assert b["seq"] == src["batch_seq"]
        dr, dc = rotate_offset(src["local_row"], src["local_col"], b["rotation_deg"])
        assert (b["origin_row"] + dr, b["origin_col"] + dc) == (c["row"], c["col"])


def test_published_map_and_sources_come_from_one_snapshot(db_path: str) -> None:
    import sqlite3 as sqlite

    from app.schemas import BatchRequest

    service = WaferService(db_path)
    for i, machine in enumerate(("M1", "M2", "M3")):
        service.accept_batch(
            "W",
            BatchRequest(**batch(machine=machine, origin=(i, 0), grid=[[cell("PASS", i)]])),
        )
    service.recompute("W")

    conn = sqlite.connect(db_path)
    try:
        computed = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT computed_seq FROM map_cells WHERE wafer_id = 'W'"
            )
        }
        committed = conn.execute(
            "SELECT committed_seq FROM wafers WHERE wafer_id = 'W'"
        ).fetchone()[0]
        source_seqs = {
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT b.seq
                  FROM map_cells m JOIN batches b ON b.batch_id = m.source_batch_id
                 WHERE m.wafer_id = 'W'
                """
            )
        }
    finally:
        conn.close()
    assert computed == {committed} == {3}
    assert source_seqs <= {1, 2, 3}
    # Every served cell carries a source that still exists in the batch log.
    body = service.get_map("W")
    known = {b.batch_id for b in service.list_batches("W")}
    assert {c["source"]["batch_id"] for c in body["cells"]} <= known
