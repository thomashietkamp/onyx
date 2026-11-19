"""Admin usage report: start a manual report, list the reports and the open
or failed jobs, and download a report ZIP."""

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from onyx.auth.permissions import require_permission
from onyx.background.celery.versioned_apps.client import app as client_app
from onyx.configs.constants import (
    USER_EXPORT_TASK_EXPIRES,
    OnyxCeleryPriority,
    OnyxCeleryQueues,
    OnyxCeleryTask,
)
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import Permission, TaskStatus
from onyx.db.models import User
from onyx.db.tasks import (
    fail_task_with_id,
    get_all_tasks_with_prefix,
    get_task_with_id,
    register_task,
)
from onyx.db.usage_report import get_usage_report_by_name, get_usage_reports
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.file_store.file_store import get_default_file_store
from onyx.server.usage_report.generation import (
    usage_report_download_name,
    usage_report_name,
)
from onyx.server.usage_report.jobs import (
    USAGE_REPORT_TASK_NAME_PREFIX,
    usage_report_task_name,
)
from onyx.server.usage_report.models import (
    GenerateUsageReportParams,
    UsageReportJob,
    UsageReportMetadata,
)
from onyx.utils.logger import setup_logger
from shared_configs.contextvars import get_current_tenant_id

logger = setup_logger()

router = APIRouter(prefix="/admin")


@router.post("/usage-report")
def generate_usage_report(
    params: GenerateUsageReportParams,
    user: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
    db_session: Session = Depends(get_session),
) -> UsageReportJob:
    report_id = str(params.report_id)
    if get_task_with_id(db_session, report_id) is not None or (
        get_usage_report_by_name(db_session, usage_report_name(report_id)) is not None
    ):
        raise OnyxError(OnyxErrorCode.CONFLICT, "A report with this id exists")

    job = register_task(
        usage_report_task_name(report_id), db_session, task_id=report_id
    )
    try:
        client_app.send_task(
            OnyxCeleryTask.GENERATE_USAGE_REPORT_TASK,
            kwargs={
                "tenant_id": get_current_tenant_id(),
                "report_id": report_id,
                "requestor_user_id": str(user.id),
                "period_from": (
                    params.period_from.isoformat() if params.period_from else None
                ),
                "period_to": params.period_to.isoformat() if params.period_to else None,
            },
            queue=OnyxCeleryQueues.PRIMARY,
            priority=OnyxCeleryPriority.MEDIUM,
            # The hourly cleanup fails the job once this and the grace pass.
            expires=USER_EXPORT_TASK_EXPIRES,
        )
    except Exception:
        logger.exception("Failed to send usage report task %s", report_id)
        fail_task_with_id(db_session, report_id, TaskStatus.PENDING)
        raise OnyxError(
            OnyxErrorCode.SERVICE_UNAVAILABLE, "Could not start the usage report"
        )

    return UsageReportJob(
        report_id=report_id, status=job.status, time_created=job.register_time
    )


@router.get("/usage-report")
def list_usage_reports(
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
    db_session: Session = Depends(get_session),
) -> list[UsageReportMetadata]:
    return [
        UsageReportMetadata(
            report_name=report.report_name,
            requestor=report.requestor.email if report.requestor else None,
            time_created=report.time_created,
            period_from=report.period_from,
            period_to=report.period_to,
        )
        for report in get_usage_reports(db_session)
    ]


@router.get("/usage-report-jobs")
def list_usage_report_jobs(
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
    db_session: Session = Depends(get_session),
) -> list[UsageReportJob]:
    jobs = get_all_tasks_with_prefix(db_session, USAGE_REPORT_TASK_NAME_PREFIX)
    jobs.sort(key=lambda job: job.register_time, reverse=True)
    return [
        UsageReportJob(
            report_id=job.task_id, status=job.status, time_created=job.register_time
        )
        for job in jobs
        # A published job has no row. A SUCCESS row is a leftover.
        if job.status != TaskStatus.SUCCESS
    ]


@router.get("/usage-report/{report_name}")
def read_usage_report(
    report_name: str,
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
    db_session: Session = Depends(get_session),
) -> StreamingResponse:
    report = get_usage_report_by_name(db_session, report_name)
    if report is None:
        raise OnyxError(OnyxErrorCode.NOT_FOUND, "Usage report not found")

    file_io = get_default_file_store().read_file(report.report_name, mode="b")
    download_name = usage_report_download_name(report.period_from, report.period_to)
    return StreamingResponse(
        file_io,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )
