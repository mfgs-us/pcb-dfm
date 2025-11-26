from __future__ import annotations

from typing import List, Optional, Dict, Any, Union
from pydantic import BaseModel, Field
from datetime import datetime


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
    status: str = Field(regex="^(pass|warning|fail)$")
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
    severity: str = Field(regex="^(info|warning|error|critical)$")
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


class CheckResult(BaseModel):
    check_id: str
    name: Optional[str] = None
    category_id: Optional[str] = None

    status: str = Field(regex="^(pass|warning|fail|not_applicable)$")
    severity: str = Field(regex="^(info|warning|error|critical)$")

    score: Optional[float] = Field(default=None, ge=0, le=100)
    metric: Optional[MetricResult] = None
    violations: List[Violation] = Field(default_factory=list)


class CategoryResult(BaseModel):
    category_id: str
    name: Optional[str] = None

    score: Optional[float] = Field(default=None, ge=0, le=100)
    status: Optional[str] = Field(default=None, regex="^(pass|warning|fail)$")
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
