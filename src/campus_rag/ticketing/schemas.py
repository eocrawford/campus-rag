"""Request and response models for the mock ticketing API."""

from datetime import datetime
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

TicketCategory = Literal["policy-question", "it-help", "other"]
TICKET_CATEGORIES: tuple[str, ...] = get_args(TicketCategory)
TicketStatus = Literal["open", "closed"]

SUMMARY_MIN_CHARS = 5
SUMMARY_MAX_CHARS = 200
DETAILS_MAX_CHARS = 4000
QUESTION_MAX_CHARS = 2000


class TicketCreate(BaseModel):
    category: TicketCategory = Field(description="Which queue the ticket belongs to.")
    summary: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=SUMMARY_MIN_CHARS, max_length=SUMMARY_MAX_CHARS
        ),
    ] = Field(description="One-line description.")
    details: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=DETAILS_MAX_CHARS)
    ] = Field(description="What the person asked and what is needed.")
    source_question: Annotated[str, StringConstraints(max_length=QUESTION_MAX_CHARS)] | None = (
        Field(None, description="The question that could not be answered, if any.")
    )
    request_id: int | None = Field(
        None, ge=1, description="The `request_id` of the answer that prompted the ticket."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "category": "policy-question",
                    "summary": "Policy question not answered: campus bus passes",
                    "details": "The policy assistant found no policy on student bus passes.",
                    "source_question": "Is there a bus pass for graduate students?",
                    "request_id": 12,
                }
            ]
        }
    )


class Ticket(BaseModel):
    id: int
    url: str = Field(description="Where to read this ticket.")
    created_at: datetime
    status: TicketStatus
    category: str
    summary: str
    details: str
    source_question: str | None
    request_id: int | None


class TicketList(BaseModel):
    tickets: list[Ticket]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    detail: str


class ErrorResponse(BaseModel):
    detail: str
