"""The manual usage report against real Postgres and the file store: the job
row lifecycle, the ZIP contents, and the hourly cleanup of lost jobs."""

import csv
import io
import zipfile
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import IO
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from onyx.configs.constants import USER_EXPORT_TASK_EXPIRES
from onyx.db.enums import TaskStatus
from onyx.db.llm_usage import LLMUsageRecord
from onyx.db.models import TaskQueueState, UsageReport, User, UserUsage
from onyx.db.tasks import fail_task_with_id, get_task_with_id
from onyx.db.user_usage import record_user_usage
from onyx.file_store.file_store import get_default_file_store
from onyx.server.usage_report import generation
from onyx.server.usage_report.api import (
    generate_usage_report,
    list_usage_report_jobs,
    list_usage_reports,
    read_usage_report,
)
from onyx.server.usage_report.generation import usage_report_name
from onyx.server.usage_report.jobs import (
    cleanup_usage_report_jobs,
    run_usage_report_job,
    usage_report_task_name,
)
from onyx.server.usage_report.models import GenerateUsageReportParams
from tests.external_dependency_unit.conftest import MakeTestUser

# A day long before any real usage, so other rows in the database do not
# reach the report.
_REPORT_DAY = datetime(2001, 2, 3, tzinfo=UTC)


@pytest.fixture
def admin_user(
    db_session: Session,
    tenant_context: None,  # noqa: ARG001
    make_test_user: MakeTestUser,
) -> Generator[User, None, None]:
    user = make_test_user("usage_report_admin", is_admin=True)
    model = f"test-model-{uuid4().hex[:8]}"
    record_user_usage(
        db_session,
        str(user.id),
        LLMUsageRecord(
            model=model,
            flow="chat",
            provider="openai",
            model_configuration_id=None,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=0,
            cost_cents=12.5,
            window_start=_REPORT_DAY,
        ),
    )
    db_session.commit()
    yield user
    db_session.execute(delete(UserUsage).where(UserUsage.model == model))
    db_session.commit()


def _start_report(db_session: Session, user: User) -> tuple[str, MagicMock]:
    report_id = uuid4()
    with patch("onyx.server.usage_report.api.client_app") as client_app:
        generate_usage_report(
            GenerateUsageReportParams(
                period_from=_REPORT_DAY, period_to=_REPORT_DAY, report_id=report_id
            ),
            user=user,
            db_session=db_session,
        )
    return str(report_id), client_app


def _run_task(report_id: str, requestor: User) -> None:
    run_usage_report_job(
        report_id=report_id,
        requestor_user_id=str(requestor.id),
        period_from=_REPORT_DAY.isoformat(),
        period_to=_REPORT_DAY.isoformat(),
    )


def _cleanup_report(db_session: Session, report_id: str) -> None:
    db_session.rollback()
    db_session.execute(
        delete(TaskQueueState).where(TaskQueueState.task_id == report_id)
    )
    db_session.execute(
        delete(UsageReport).where(
            UsageReport.report_name == usage_report_name(report_id)
        )
    )
    db_session.commit()
    get_default_file_store().delete_file(
        usage_report_name(report_id), error_on_missing=False
    )


def test_manual_report_publishes_zip_and_drops_job(
    db_session: Session, admin_user: User
) -> None:
    report_id, client_app = _start_report(db_session, admin_user)
    try:
        send_kwargs = client_app.send_task.call_args.kwargs
        assert send_kwargs["expires"] == USER_EXPORT_TASK_EXPIRES
        assert send_kwargs["kwargs"]["report_id"] == report_id

        jobs = list_usage_report_jobs(_=admin_user, db_session=db_session)
        assert [
            (job.report_id, job.status) for job in jobs if job.report_id == report_id
        ] == [(report_id, TaskStatus.PENDING)]

        _run_task(report_id, admin_user)
        db_session.expire_all()

        jobs = list_usage_report_jobs(_=admin_user, db_session=db_session)
        assert all(job.report_id != report_id for job in jobs)
        reports = [
            report
            for report in list_usage_reports(_=admin_user, db_session=db_session)
            if report_id in report.report_name
        ]
        assert len(reports) == 1
        assert reports[0].requestor == admin_user.email

        response = read_usage_report(
            reports[0].report_name, _=admin_user, db_session=db_session
        )
        assert response.media_type == "application/zip"
        file_io = get_default_file_store().read_file(reports[0].report_name, mode="b")
        with zipfile.ZipFile(io.BytesIO(file_io.read())) as archive:
            names = set(archive.namelist())
            assert names == {
                "usage_report.pdf",
                "chat_messages.csv",
                "users.csv",
                "usage_by_user.csv",
                "usage_by_system.csv",
            }
            assert archive.read("usage_report.pdf").startswith(b"%PDF")
            usage_rows = list(
                csv.DictReader(io.StringIO(archive.read("usage_by_user.csv").decode()))
            )
            own_rows = [
                row for row in usage_rows if row["user_email"] == admin_user.email
            ]
            assert len(own_rows) == 1
            assert own_rows[0]["day"] == "2001-02-03"
            assert own_rows[0]["input_tokens"] == "100"
            user_ids = {
                row["user_id"]
                for row in csv.DictReader(
                    io.StringIO(archive.read("users.csv").decode())
                )
            }
            assert str(admin_user.id) in user_ids
    finally:
        _cleanup_report(db_session, report_id)


def test_late_worker_deletes_its_file(db_session: Session, admin_user: User) -> None:
    """The cleanup failed the job while the worker built the ZIP. The worker
    must not publish, and must delete its upload."""
    report_id, _ = _start_report(db_session, admin_user)
    real_generate = generation.generate_usage_report_zip

    def generate_then_lose_job(
        session: Session, period_from: datetime | None, period_to: datetime | None
    ) -> IO[bytes]:
        report_zip = real_generate(session, period_from, period_to)
        fail_task_with_id(session, report_id, TaskStatus.STARTED)
        return report_zip

    try:
        with patch(
            "onyx.server.usage_report.jobs.generate_usage_report_zip",
            side_effect=generate_then_lose_job,
        ):
            _run_task(report_id, admin_user)
        db_session.expire_all()

        job = get_task_with_id(db_session, report_id)
        assert job is not None and job.status == TaskStatus.FAILURE
        assert all(
            report_id not in report.report_name
            for report in list_usage_reports(_=admin_user, db_session=db_session)
        )
        with pytest.raises(RuntimeError):
            get_default_file_store().read_file_record(usage_report_name(report_id))
    finally:
        _cleanup_report(db_session, report_id)


def test_cleanup_fails_lost_jobs_and_purges_old_failures(
    db_session: Session, admin_user: User
) -> None:
    lost_id, _ = _start_report(db_session, admin_user)
    expired_id, _ = _start_report(db_session, admin_user)
    fresh_id, _ = _start_report(db_session, admin_user)
    now = datetime.now(UTC)
    try:
        # The task of this job expired in the queue two hours ago.
        db_session.execute(
            update(TaskQueueState)
            .where(TaskQueueState.task_id == lost_id)
            .values(register_time=now - timedelta(hours=2))
        )
        # This job failed two days ago.
        db_session.execute(
            update(TaskQueueState)
            .where(TaskQueueState.task_id == expired_id)
            .values(status=TaskStatus.FAILURE, start_time=now - timedelta(days=2))
        )
        db_session.commit()

        cleanup_usage_report_jobs(db_session)
        db_session.expire_all()

        lost = get_task_with_id(db_session, lost_id)
        assert lost is not None and lost.status == TaskStatus.FAILURE
        assert get_task_with_id(db_session, expired_id) is None
        fresh = get_task_with_id(db_session, fresh_id)
        assert fresh is not None and fresh.status == TaskStatus.PENDING
        assert fresh.task_name == usage_report_task_name(fresh_id)
    finally:
        for report_id in (lost_id, expired_id, fresh_id):
            _cleanup_report(db_session, report_id)
