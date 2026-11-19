from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, model_validator

from onyx.db.enums import TaskStatus


class GenerateUsageReportParams(BaseModel):
    """Both period bounds or neither. No bounds means all time. `period_to` is
    the start of the last day of the period, so the period includes that day."""

    period_from: datetime | None = None
    period_to: datetime | None = None
    # The client picks the id, so it can find its job and report in the lists.
    report_id: UUID

    @model_validator(mode="after")
    def _check_period(self) -> "GenerateUsageReportParams":
        if (self.period_from is None) != (self.period_to is None):
            raise ValueError("Give both period_from and period_to, or neither")
        if (
            self.period_from is not None
            and self.period_to is not None
            and self.period_from > self.period_to
        ):
            raise ValueError("period_from must not be after period_to")
        return self


class UsageReportMetadata(BaseModel):
    report_name: str
    # The email of the admin who asked for the report. None for a report that
    # no person asked for.
    requestor: str | None
    time_created: datetime
    period_from: datetime | None
    period_to: datetime | None


class UsageReportJob(BaseModel):
    """A manual report that is not published yet: pending, started or failed."""

    report_id: str
    status: TaskStatus
    time_created: datetime
