"""Request/response models and their invariants."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
MAX_WAFER_DIM = 2000
MAX_LOCAL_DIM = 2000
MAX_CELLS = 250_000
MAX_RETEST_NO = 1_000_000_000

WaferId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=ID_PATTERN
    ),
]

Verdict = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
Identifier = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
Rotation = Literal[0, 90, 180, 270]


class LocalCell(BaseModel):
    """One measured cell inside the local probe grid."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict = Field(
        description="Opaque measurement verdict, e.g. PASS / FAIL / 1 / 0.",
        examples=["PASS"],
    )
    retest_no: int = Field(
        ge=0,
        le=MAX_RETEST_NO,
        description="Retest counter. At one wafer position the highest number wins.",
        examples=[0],
    )


class BatchRequest(BaseModel):
    """A single probe-machine upload of a rotated local grid."""

    model_config = ConfigDict(extra="forbid")

    wafer_rows: int = Field(ge=1, le=MAX_WAFER_DIM, description="Full wafer row count.")
    wafer_cols: int = Field(ge=1, le=MAX_WAFER_DIM, description="Full wafer column count.")
    machine_id: Identifier = Field(description="Probe machine identifier.")
    origin_row: int = Field(
        ge=0,
        le=MAX_WAFER_DIM - 1,
        description="Full-wafer row occupied by the local grid's (0,0) cell.",
    )
    origin_col: int = Field(
        ge=0,
        le=MAX_WAFER_DIM - 1,
        description="Full-wafer column occupied by the local grid's (0,0) cell.",
    )
    rotation_deg: Rotation = Field(
        description="Clockwise rotation of the local grid around its (0,0) cell.",
    )
    local_grid: list[list[LocalCell | None]] = Field(
        description=(
            "Rectangular local grid. A null cell means that position was not "
            "measured by this upload; it does not clear an existing result."
        ),
        min_length=1,
    )

    @model_validator(mode="after")
    def _validate_shape_and_origin(self) -> "BatchRequest":
        local_rows = len(self.local_grid)
        local_cols = len(self.local_grid[0])
        if local_rows > MAX_LOCAL_DIM or local_cols > MAX_LOCAL_DIM:
            raise ValueError(
                f"local grid must be at most {MAX_LOCAL_DIM}x{MAX_LOCAL_DIM} cells"
            )
        if local_rows * local_cols > MAX_CELLS:
            raise ValueError(f"local grid must contain at most {MAX_CELLS} cells")
        for row_no, row in enumerate(self.local_grid):
            if len(row) != local_cols:
                raise ValueError(
                    f"local_grid is ragged: row {row_no} has {len(row)} cells, "
                    f"expected {local_cols}"
                )
        if not any(cell is not None for row in self.local_grid for cell in row):
            raise ValueError("local_grid must contain at least one measured cell")
        if self.origin_row >= self.wafer_rows:
            raise ValueError("origin_row must be inside the wafer")
        if self.origin_col >= self.wafer_cols:
            raise ValueError("origin_col must be inside the wafer")
        return self


class SourceInfo(BaseModel):
    """Where the winning measurement for a cell came from."""

    machine_id: str
    batch_id: str
    batch_seq: int
    local_row: int
    local_col: int


class CellInfo(BaseModel):
    row: int
    col: int
    verdict: str
    retest_no: int
    source: SourceInfo


class RecomputeError(BaseModel):
    seq: int
    message: str
    at: str


class MapResponse(BaseModel):
    wafer_id: str
    rows: int
    cols: int
    status: Literal["empty", "pending_update", "ready"]
    pending_update: bool = Field(
        description="True when accepted data is newer than the served map."
    )
    recompute_failed: bool = Field(
        description=(
            "True when the latest accepted sequence failed to publish. The "
            "previous map remains served and the wafer stays pending."
        )
    )
    accepted_seq: int = Field(description="Latest accepted batch sequence.")
    map_seq: int = Field(description="Accepted sequence the served map was built from.")
    tested_cells: int
    total_cells: int
    last_recompute_error: RecomputeError | None = None
    cells: list[CellInfo] = Field(
        description="Measured cells only. Any position not listed is untested/blank."
    )
    grid: list[list[str | None]] | None = Field(
        default=None,
        description=(
            "Dense rows x cols verdict grid (null = untested). Only present when "
            "the request asked for it; limited to 250000 cells."
        ),
    )


class BatchAcceptedResponse(BaseModel):
    wafer_id: str
    batch_id: str
    seq: int
    accepted_seq: int
    cells_accepted: int
    status: Literal["pending_update"] = "pending_update"
    recompute_scheduled: bool


class BatchInfo(BaseModel):
    batch_id: str
    seq: int
    machine_id: str
    origin_row: int
    origin_col: int
    rotation_deg: int
    local_rows: int
    local_cols: int
    measured_cells: int
    received_at: str


class BatchListResponse(BaseModel):
    wafer_id: str
    batches: list[BatchInfo]


class CellLookupResponse(BaseModel):
    wafer_id: str
    row: int
    col: int
    tested: bool = Field(description="False means untested/blank.")
    status: Literal["empty", "pending_update", "ready"]
    accepted_seq: int
    map_seq: int
    cell: CellInfo | None = None


class WaferSummary(BaseModel):
    wafer_id: str
    rows: int
    cols: int
    status: Literal["empty", "pending_update", "ready"]
    recompute_failed: bool
    pending_update: bool
    accepted_seq: int
    map_seq: int


class WaferListResponse(BaseModel):
    wafers: list[WaferSummary]


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail


class RecomputeResponse(BaseModel):
    wafer_id: str
    accepted_seq: int
    map_seq: int
    status: Literal["empty", "pending_update", "ready"]
    recompute_failed: bool = False
    scheduled: bool


class HealthResponse(BaseModel):
    status: Literal["ok"]
    database: str
