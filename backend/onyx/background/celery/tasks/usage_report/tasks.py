"""Generates a manual usage report (see onyx/server/usage_report/jobs.py)."""

from celery import Task, shared_task

from onyx.configs.app_configs import JOB_TIMEOUT
from onyx.configs.constants import OnyxCeleryTask
from onyx.server.usage_report.jobs import run_usage_report_job


@shared_task(  # ty: ignore[invalid-argument-type]
    name=OnyxCeleryTask.GENERATE_USAGE_REPORT_TASK,
    ignore_result=True,
    soft_time_limit=JOB_TIMEOUT,
    bind=True,
    trail=False,
)
def generate_usage_report_task(
    self: Task,  # noqa: ARG001
    *,
    tenant_id: str,  # noqa: ARG001 - TenantAwareTask sets the tenant from it
    report_id: str,
    requestor_user_id: str | None,
    period_from: str | None,
    period_to: str | None,
) -> None:
    run_usage_report_job(report_id, requestor_user_id, period_from, period_to)
