from __future__ import annotations

from typing import ClassVar, Optional, Union, List, Dict, TYPE_CHECKING, Any
from pydantic import BaseModel, Field, model_validator
from datetime import datetime


SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}


def _max_severity(sevs: List[str]) -> str:
    if not sevs:
        return "info"
    return max(sevs, key=lambda s: SEVERITY_RANK.get(s, 0))


class RunInfo(BaseModel):
    id: str
    generated_at: datetime
    tool: Optional[str] = None
    tool_version: Optional[str] = None


class RulesetInfo(BaseModel):
    name: str
    version: str


class BoardSize(BaseModel):
    width: float
    height: float


class DesignInfo(BaseModel):
    name: str
    revision: Optional[str] = None
    source_files: List[str] = Field(default_factory=list)
    stackup_layers: Optional[int] = None
    board_size_mm: Optional[BoardSize] = None


class SummaryCounts(BaseModel):
    info: int = 0
    warning: int = 0
    error: int = 0
    critical: int = 0


class ResultSummary(BaseModel):
    overall_score: float = Field(ge=0, le=100)
    # pydantic v2: use pattern instead of regex
    status: str = Field(pattern="^(pass|warning|fail)$")
    violations_total: int = 0
    violations_by_severity: SummaryCounts = Field(default_factory=SummaryCounts)


class ViolationLocation(BaseModel):
    layer: Optional[str] = None
    x_mm: Optional[float] = None
    y_mm: Optional[float] = None
    width_mm: Optional[float] = None
    height_mm: Optional[float] = None
    net: Optional[str] = None
    component: Optional[str] = None


class Violation(BaseModel):
    message: str
    severity: str = Field(pattern="^(info|warning|error|critical)$")
    location: Optional[ViolationLocation] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


class MetricResult(BaseModel):
    kind: Optional[str] = None
    units: Optional[str] = None
    measured_value: Optional[Union[float, bool]] = None
    target: Optional[Union[float, bool]] = None
    limit_low: Optional[float] = None
    limit_high: Optional[float] = None
    margin_to_limit: Optional[float] = None

    @staticmethod
    def geometry_mm(measured_mm: float,
                    target_mm: Optional[float] = None,
                    limit_low_mm: Optional[float] = None,
                    limit_high_mm: Optional[float] = None) -> "MetricResult":
        m = MetricResult(
            kind="geometry",
            units="mm",
            measured_value=float(measured_mm),
            target=None if target_mm is None else float(target_mm),
            limit_low=None if limit_low_mm is None else float(limit_low_mm),
            limit_high=None if limit_high_mm is None else float(limit_high_mm),
        )
        # Compute margin if a bound exists
        if m.limit_low is not None:
            m.margin_to_limit = float(m.measured_value) - m.limit_low
        elif m.limit_high is not None:
            m.margin_to_limit = m.limit_high - float(m.measured_value)
        return m

    @staticmethod
    def ratio_percent(measured_pct: float,
                      target_pct: Optional[float] = None,
                      limit_high_pct: Optional[float] = None) -> "MetricResult":
        m = MetricResult(
            kind="ratio",
            units="%",
            measured_value=float(measured_pct),
            target=None if target_pct is None else float(target_pct),
            limit_low=None,
            limit_high=None if limit_high_pct is None else float(limit_high_pct),
        )
        if m.limit_high is not None:
            m.margin_to_limit = m.limit_high - float(m.measured_value)
        return m

    @model_validator(mode="after")
    def _validate_units_vs_values(self):
        # Only enforce when kind and units are set and measured_value is numeric
        if self.kind == "geometry" and isinstance(self.measured_value, (int, float)):
            if self.units == "um":
                # Typical PCB geometry in um is hundreds to tens of thousands
                # If it's < 10, it's almost certainly mm mislabeled as um
                if float(self.measured_value) < 10.0:
                    raise ValueError("geometry metric units='um' but measured_value looks like mm scale")
            if self.units == "mm":
                # Typical PCB geometry in mm is < 100
                if float(self.measured_value) > 1000.0:
                    raise ValueError("geometry metric units='mm' but measured_value looks like um scale")
        if self.kind == "ratio" and self.units not in (None, "%"):
            raise ValueError("ratio metrics must use units='%'")
        return self


class CheckResult(BaseModel):
    check_id: str
    name: Optional[str] = None
    category_id: Optional[str] = None

    status: str = Field(pattern="^(pass|warning|fail|not_applicable)$")
    severity: Optional[str] = Field(pattern="^(info|warning|error|critical)$")

    score: Optional[float] = Field(default=None, ge=0, le=100)
    metric: Optional[MetricResult] = None
    violations: List[Violation] = Field(default_factory=list)

    def finalize(self) -> "CheckResult":
        # Create a copy with normalized values
        # Normalize severity from violations or status
        violation_sevs = [v.severity for v in self.violations if getattr(v, "severity", None)]
        if violation_sevs:
            final_severity = _max_severity(violation_sevs)
        else:
            if self.status == "pass":
                final_severity = "info"
            elif self.status == "warning":
                final_severity = "warning"
            elif self.status == "fail":
                final_severity = "error"
            else:
                final_severity = "info"

        # Normalize score if missing
        if self.score is None:
            if self.status == "pass":
                final_score = 100.0
            elif self.status == "warning":
                final_score = 75.0
            elif self.status == "fail":
                final_score = 0.0
            else:
                final_score = 100.0
        else:
            final_score = self.score

        # Return a new instance with normalized values
        return CheckResult(
            check_id=self.check_id,
            name=self.name,
            category_id=self.category_id,
            status=self.status,
            severity=final_severity,
            score=final_score,
            metric=self.metric,
            violations=self.violations,
        )


class CategoryResult(BaseModel):
    category_id: str
    name: Optional[str] = None

    score: Optional[float] = Field(default=None, ge=0, le=100)
    status: Optional[str] = Field(default=None, pattern="^(pass|warning|fail)$")
    violations_count: int = 0

    checks: List[CheckResult] = Field(default_factory=list)


class DfmResult(BaseModel):
    schema_version: str = "1.0.0"
    run: RunInfo
    ruleset: RulesetInfo
    design: DesignInfo
    summary: ResultSummary
    categories: List[CategoryResult]

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, data: str) -> "DfmResult":
        return cls.model_validate_json(data)


# Fix forward references by rebuilding models
CheckResult.model_rebuild()
CategoryResult.model_rebuild()
DfmResult.model_rebuild()
