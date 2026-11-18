"""Geometry: place a rotated local probe grid onto the full wafer map.

Coordinate convention
---------------------
Full-wafer positions are zero based: rows grow downwards (south), columns grow
rightwards (east).  The uploaded local ``(0, 0)`` cell is placed at
``(origin_row, origin_col)`` on the full wafer.  A local offset ``(dr, dc)`` is
rotated clockwise around that origin and then added to it.

Clockwise rotation in this screen-like coordinate system is::

    90 deg :  (dr, dc) -> ( dc, -dr)
    180 deg:  (dr, dc) -> (-dr, -dc)
    270 deg:  (dr, dc) -> (-dc,  dr)

For instance, the cell immediately south of the origin ``(1, 0)`` moves west
``(0, -1)`` after a 90 degree clockwise rotation, which is what one expects
when physically turning a wafer clockwise.
"""

from __future__ import annotations

from dataclasses import dataclass

VALID_ROTATIONS = (0, 90, 180, 270)


@dataclass(frozen=True)
class LocalMeasurement:
    """One measured local-grid cell (local coordinates are zero based)."""

    local_row: int
    local_col: int
    verdict: str
    retest_no: int


@dataclass(frozen=True)
class MappedMeasurement:
    """A local measurement with its resolved full-wafer position."""

    local_row: int
    local_col: int
    full_row: int
    full_col: int
    verdict: str
    retest_no: int


def rotate_offset(dr: int, dc: int, rotation_deg: int) -> tuple[int, int]:
    """Rotate a local offset clockwise by ``rotation_deg`` around (0, 0)."""
    if rotation_deg == 0:
        return dr, dc
    if rotation_deg == 90:
        return dc, -dr
    if rotation_deg == 180:
        return -dr, -dc
    if rotation_deg == 270:
        return -dc, dr
    raise ValueError(f"unsupported rotation: {rotation_deg}")


def map_local_to_full(
    measurements: list[LocalMeasurement],
    *,
    origin_row: int,
    origin_col: int,
    rotation_deg: int,
) -> list[MappedMeasurement]:
    """Map every local measurement onto full-wafer coordinates.

    Bounds are intentionally *not* checked here; the service reports all
    offending cells in one batch rejection.
    """
    if rotation_deg not in VALID_ROTATIONS:
        raise ValueError(f"unsupported rotation: {rotation_deg}")

    mapped: list[MappedMeasurement] = []
    for item in measurements:
        dr, dc = rotate_offset(item.local_row, item.local_col, rotation_deg)
        mapped.append(
            MappedMeasurement(
                local_row=item.local_row,
                local_col=item.local_col,
                full_row=origin_row + dr,
                full_col=origin_col + dc,
                verdict=item.verdict,
                retest_no=item.retest_no,
            )
        )
    return mapped
