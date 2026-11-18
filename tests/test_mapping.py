"""Pure geometry tests."""

from __future__ import annotations

import pytest

from app.mapping import LocalMeasurement, map_local_to_full, rotate_offset


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, (1, 2)),
        (90, (2, -1)),
        (180, (-1, -2)),
        (270, (-2, 1)),
    ],
)
def test_rotate_offset_clockwise(rotation: int, expected: tuple[int, int]) -> None:
    assert rotate_offset(1, 2, rotation) == expected


def test_rotations_compose_back_to_identity() -> None:
    offset = (3, -2)
    for rotation in (0, 90, 180, 270):
        once = rotate_offset(*offset, rotation)
        four_times = once
        for _ in range(3):
            four_times = rotate_offset(*four_times, rotation)
        assert four_times == offset


def test_local_origin_stays_at_origin_for_every_rotation() -> None:
    for rotation in (0, 90, 180, 270):
        assert rotate_offset(0, 0, rotation) == (0, 0)


def test_map_places_cells_around_origin() -> None:
    mapped = map_local_to_full(
        [
            LocalMeasurement(0, 0, "PASS", 0),
            LocalMeasurement(0, 1, "FAIL", 0),
            LocalMeasurement(1, 0, "PASS", 1),
        ],
        origin_row=10,
        origin_col=20,
        rotation_deg=270,
    )
    positions = {(m.local_row, m.local_col): (m.full_row, m.full_col) for m in mapped}
    assert positions[(0, 0)] == (10, 20)
    assert positions[(0, 1)] == (9, 20)  # 270 cw turns east into north
    assert positions[(1, 0)] == (10, 21)  # and south into east


def test_invalid_rotation_raises() -> None:
    with pytest.raises(ValueError):
        rotate_offset(1, 1, 45)
