"""The PDF summary of the usage report. It reads the same usage rows as the
usage CSVs, so the totals agree."""

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape

from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Flowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from onyx.db.system_usage import SystemUsageExportRow
from onyx.db.usage_report import UsageReportUserRow
from onyx.db.user_usage import DELETED_USER_EXPORT_EMAIL, UsageExportRow

TOP_ENTRY_COUNT = 10
# The PDF lists this many unused seats. The count covers all of them.
MAX_LISTED_UNUSED_SEATS = 50
# A period with more days than this shows spend for each month.
MAX_CHART_DAYS = 62

_TABLE_STYLE = TableStyle(
    [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEEEE")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#BBBBBB")),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
)


class SpendBucket(BaseModel):
    label: str
    user_cost_cents: float
    system_cost_cents: float


class RankedSpend(BaseModel):
    name: str
    tokens: int
    cost_cents: float


class UsageReportData(BaseModel):
    period_label: str
    generated_at: datetime
    active_people: int
    seats: int
    unused_seat_count: int
    listed_unused_seats: list[str]
    user_cost_cents: float
    system_cost_cents: float
    user_tokens: int
    system_tokens: int
    spend_over_time: list[SpendBucket]
    top_models: list[RankedSpend]
    top_users: list[RankedSpend]


def _row_tokens(row: UsageExportRow | SystemUsageExportRow) -> int:
    return (
        row.input_tokens
        + row.output_tokens
        + row.cache_read_tokens
        + row.cache_creation_tokens
    )


def _ranked(
    tokens_by_name: dict[str, int], cost_by_name: dict[str, float]
) -> list[RankedSpend]:
    entries = [
        RankedSpend(name=name, tokens=tokens_by_name[name], cost_cents=cost)
        for name, cost in cost_by_name.items()
    ]
    entries.sort(key=lambda entry: (-entry.cost_cents, -entry.tokens, entry.name))
    return entries[:TOP_ENTRY_COUNT]


def _spend_over_time(
    user_rows: Sequence[UsageExportRow], system_rows: Sequence[SystemUsageExportRow]
) -> list[SpendBucket]:
    days = {row.day for row in user_rows} | {row.day for row in system_rows}
    # Days are "YYYY-MM-DD", so the first 7 characters name the month.
    label_length = 7 if len(days) > MAX_CHART_DAYS else 10
    user_cost: dict[str, float] = defaultdict(float)
    system_cost: dict[str, float] = defaultdict(float)
    for row in user_rows:
        user_cost[row.day[:label_length]] += row.cost_cents
    for row in system_rows:
        system_cost[row.day[:label_length]] += row.cost_cents
    return [
        SpendBucket(
            label=label,
            user_cost_cents=user_cost[label],
            system_cost_cents=system_cost[label],
        )
        for label in sorted(set(user_cost) | set(system_cost))
    ]


def build_usage_report_data(
    user_rows: Sequence[UsageExportRow],
    system_rows: Sequence[SystemUsageExportRow],
    users: Sequence[UsageReportUserRow],
    period_label: str,
    generated_at: datetime,
) -> UsageReportData:
    model_tokens: dict[str, int] = defaultdict(int)
    model_cost: dict[str, float] = defaultdict(float)
    user_tokens: dict[str, int] = defaultdict(int)
    user_cost: dict[str, float] = defaultdict(float)
    for row in user_rows:
        model_tokens[row.model] += _row_tokens(row)
        model_cost[row.model] += row.cost_cents
        user_tokens[row.email] += _row_tokens(row)
        user_cost[row.email] += row.cost_cents
    for system_row in system_rows:
        model_tokens[system_row.model] += _row_tokens(system_row)
        model_cost[system_row.model] += system_row.cost_cents

    # System work is not activity of a person, so only user rows count here.
    active_emails = {
        email
        for email, tokens in user_tokens.items()
        if email != DELETED_USER_EXPORT_EMAIL and (tokens > 0 or user_cost[email] > 0)
    }
    seat_emails = [user.email for user in users if user.is_active]
    unused_seats = [email for email in seat_emails if email not in active_emails]

    return UsageReportData(
        period_label=period_label,
        generated_at=generated_at,
        active_people=len(active_emails),
        seats=len(seat_emails),
        unused_seat_count=len(unused_seats),
        listed_unused_seats=unused_seats[:MAX_LISTED_UNUSED_SEATS],
        user_cost_cents=sum(row.cost_cents for row in user_rows),
        system_cost_cents=sum(row.cost_cents for row in system_rows),
        user_tokens=sum(_row_tokens(row) for row in user_rows),
        system_tokens=sum(_row_tokens(row) for row in system_rows),
        spend_over_time=_spend_over_time(user_rows, system_rows),
        top_models=_ranked(model_tokens, model_cost),
        top_users=_ranked(user_tokens, user_cost),
    )


def _dollars(cost_cents: float) -> str:
    return f"${cost_cents / 100:,.2f}"


def _ranked_table(title: str, entries: Sequence[RankedSpend]) -> Table:
    rows: list[list[str]] = [[title, "Tokens", "Cost"]]
    rows.extend(
        [entry.name, f"{entry.tokens:,}", _dollars(entry.cost_cents)]
        for entry in entries
    )
    table = Table(rows, colWidths=[9 * cm, 3.5 * cm, 3.5 * cm], repeatRows=1)
    table.setStyle(_TABLE_STYLE)
    return table


def _spend_table(buckets: Sequence[SpendBucket]) -> Table:
    rows: list[list[str]] = [["Period", "User spend", "System spend", "Total"]]
    rows.extend(
        [
            bucket.label,
            _dollars(bucket.user_cost_cents),
            _dollars(bucket.system_cost_cents),
            _dollars(bucket.user_cost_cents + bucket.system_cost_cents),
        ]
        for bucket in buckets
    )
    table = Table(rows, colWidths=[4 * cm, 4 * cm, 4 * cm, 4 * cm], repeatRows=1)
    table.setStyle(_TABLE_STYLE)
    return table


def render_usage_report_pdf(data: UsageReportData) -> bytes:
    styles = getSampleStyleSheet()
    story: list[Flowable] = [
        Paragraph("Onyx usage report", styles["Title"]),
        Paragraph(
            f"Period: {escape(data.period_label)}. Generated "
            f"{data.generated_at:%Y-%m-%d %H:%M} UTC. Days are in UTC.",
            styles["Normal"],
        ),
        Spacer(1, 0.5 * cm),
    ]

    summary = Table(
        [
            ["Active people", f"{data.active_people:,}"],
            ["Active seats", f"{data.seats:,}"],
            ["Unused seats", f"{data.unused_seat_count:,}"],
            ["User spend", _dollars(data.user_cost_cents)],
            ["System spend", _dollars(data.system_cost_cents)],
            ["Total spend", _dollars(data.user_cost_cents + data.system_cost_cents)],
            ["User tokens", f"{data.user_tokens:,}"],
            ["System tokens", f"{data.system_tokens:,}"],
        ],
        colWidths=[9 * cm, 7 * cm],
    )
    summary.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#BBBBBB")),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ]
        )
    )
    story += [summary, Spacer(1, 0.5 * cm)]

    story.append(Paragraph("Spend over time", styles["Heading2"]))
    if data.spend_over_time:
        story.append(_spend_table(data.spend_over_time))
    else:
        story.append(Paragraph("No usage in this period.", styles["Normal"]))

    story.append(Paragraph("Top models", styles["Heading2"]))
    story.append(_ranked_table("Model", data.top_models))
    story.append(Paragraph("Top users", styles["Heading2"]))
    story.append(_ranked_table("User", data.top_users))

    story.append(Paragraph("Unused seats", styles["Heading2"]))
    if data.listed_unused_seats:
        story.append(
            Paragraph(
                f"{data.unused_seat_count:,} active accounts had no usage in "
                "this period.",
                styles["Normal"],
            )
        )
        for email in data.listed_unused_seats:
            # Paragraph text is markup, so escape the address.
            story.append(Paragraph(escape(email), styles["Normal"]))
        hidden = data.unused_seat_count - len(data.listed_unused_seats)
        if hidden > 0:
            story.append(
                Paragraph(f"And {hidden:,} more. See users.csv.", styles["Normal"])
            )
    else:
        story.append(Paragraph("Every active account had usage.", styles["Normal"]))

    buffer = BytesIO()
    SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title="Onyx usage report",
        leftMargin=2 * cm,
        rightMargin=2 * cm,
    ).build(story)
    return buffer.getvalue()
