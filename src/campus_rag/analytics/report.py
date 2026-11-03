"""Summarize the request log and ratings of a recent period."""

from dataclasses import dataclass
from datetime import date, datetime

import psycopg

_WINDOW = "created_at >= now() - make_interval(days => %(days)s)"


@dataclass(frozen=True)
class VolumeRow:
    day: date
    interface: str
    endpoint: str
    requests: int


@dataclass(frozen=True)
class LatencyRow:
    interface: str
    endpoint: str
    requests: int
    p50_ms: float
    p95_ms: float


@dataclass(frozen=True)
class NegativeQuestion:
    rated_at: datetime
    question: str
    comment: str | None


@dataclass(frozen=True)
class Report:
    days: int
    total_requests: int
    volume: tuple[VolumeRow, ...]  # by day, interface, and endpoint, oldest day first
    latency: tuple[LatencyRow, ...]  # successful requests only
    answers: int  # successful requests that produced an answer or a refusal
    refused: int
    mean_citations: float | None  # over answers that were not refused
    errors: int
    feedback: int
    positive_feedback: int
    negative_questions: tuple[NegativeQuestion, ...]  # most recently rated first

    @property
    def refusal_rate(self) -> float | None:
        return self.refused / self.answers if self.answers else None

    @property
    def positive_share(self) -> float | None:
        return self.positive_feedback / self.feedback if self.feedback else None


def build_report(conn: psycopg.Connection, days: int = 30, *, negative_limit: int = 5) -> Report:
    """Query the request log for the last `days` days."""
    if days < 1:
        raise ValueError("days must be >= 1")
    params = {"days": days}

    volume = tuple(
        VolumeRow(*row)
        for row in conn.execute(
            f"SELECT created_at::date, interface, endpoint, count(*) FROM request_log "
            f"WHERE {_WINDOW} GROUP BY 1, 2, 3 ORDER BY 1, 2, 3",
            params,
        )
    )
    latency = tuple(
        LatencyRow(interface, endpoint, requests, float(p50), float(p95))
        for interface, endpoint, requests, p50, p95 in conn.execute(
            f"SELECT interface, endpoint, count(*), "
            f"percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms), "
            f"percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) "
            f"FROM request_log WHERE {_WINDOW} AND error IS NULL AND latency_ms IS NOT NULL "
            f"GROUP BY 1, 2 ORDER BY 1, 2",
            params,
        )
    )
    answers, refused, mean_citations = conn.execute(
        f"SELECT count(*), count(*) FILTER (WHERE refused), "
        f"avg(citation_count) FILTER (WHERE NOT refused) "
        f"FROM request_log WHERE {_WINDOW} AND error IS NULL AND refused IS NOT NULL",
        params,
    ).fetchone()
    errors = conn.execute(
        f"SELECT count(*) FROM request_log WHERE {_WINDOW} AND error IS NOT NULL",
        params,
    ).fetchone()[0]
    feedback, positive = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE rating = 1) FROM answer_feedback "
        "WHERE created_at >= now() - make_interval(days => %(days)s)",
        params,
    ).fetchone()
    negatives = tuple(
        NegativeQuestion(*row)
        for row in conn.execute(
            "SELECT f.created_at, r.question, f.comment FROM answer_feedback f "
            "JOIN request_log r ON r.id = f.request_id "
            "WHERE f.rating = -1 AND f.created_at >= now() - make_interval(days => %(days)s) "
            "ORDER BY f.created_at DESC, f.id DESC LIMIT %(limit)s",
            {**params, "limit": negative_limit},
        )
    )
    return Report(
        days=days,
        total_requests=sum(row.requests for row in volume),
        volume=volume,
        latency=latency,
        answers=answers,
        refused=refused,
        mean_citations=None if mean_citations is None else float(mean_citations),
        errors=errors,
        feedback=feedback,
        positive_feedback=positive,
        negative_questions=negatives,
    )


def format_report(report: Report) -> str:
    """Plain-text tables for the terminal."""
    lines = [f"Usage, last {report.days} day(s): {report.total_requests} request(s)", ""]
    if not report.total_requests:
        lines.append("No requests logged in this period.")
        return "\n".join(lines)

    lines.append("Requests by day")
    lines.append(f"  {'day':<12}{'interface':<11}{'endpoint':<18}{'requests':>8}")
    for row in report.volume:
        lines.append(
            f"  {row.day.isoformat():<12}{row.interface:<11}{row.endpoint:<18}{row.requests:>8}"
        )

    lines += ["", "Latency of successful requests (ms)"]
    lines.append(f"  {'interface':<11}{'endpoint':<18}{'requests':>8}{'p50':>9}{'p95':>9}")
    for item in report.latency:
        lines.append(
            f"  {item.interface:<11}{item.endpoint:<18}{item.requests:>8}"
            f"{item.p50_ms:>9.0f}{item.p95_ms:>9.0f}"
        )

    lines += ["", "Answers"]
    if report.answers:
        summary = f"{report.refused} of {report.answers}"
        lines.append(f"  refusal rate: {_percent(report.refusal_rate)} ({summary})")
        if report.mean_citations is not None:
            lines.append(f"  mean citations per answered question: {report.mean_citations:.2f}")
    else:
        lines.append("  no answers logged")
    lines.append(f"  errors: {report.errors}")

    lines += ["", "Feedback"]
    if report.feedback:
        lines.append(f"  {report.feedback} rating(s), {_percent(report.positive_share)} positive")
    else:
        lines.append("  no ratings")
    if report.negative_questions:
        lines.append("  most recent negatively rated questions:")
        for item in report.negative_questions:
            stamp = item.rated_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"    {stamp}  {_one_line(item.question or '(no question text)')}")
            if item.comment:
                lines.append(f"      comment: {_one_line(item.comment)}")
    return "\n".join(lines)


def _percent(share: float | None) -> str:
    return "n/a" if share is None else f"{share * 100:.0f}%"


def _one_line(text: str, limit: int = 120) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "\u2026"
