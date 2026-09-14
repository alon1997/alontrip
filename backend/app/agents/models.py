"""Typed itinerary structures the agent returns (agent mode).

The Strands agent fills these via `structured_output_model` — the model
calls tools (search / transit / draft / replay) and its final answer is a
validated :class:`TripPlan`, not free text.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PlannedSpot(BaseModel):
    id: str = Field(description="Spot id from the catalog (search_pois)")
    name_en: str = Field(description="Display name")
    start: str = Field(description="Visit start HH:MM, 24h")
    end: str = Field(description="Visit end HH:MM, 24h (clipped by closing time)")
    note: str = Field(default="", description="One short line: why here / what to know")


class DayPlan(BaseModel):
    day: int = Field(description="1-based day number")
    city: str = Field(description="City id")
    day_start: str = Field(
        default="09:00",
        description="Clock start HH:MM for this day — arrival days use landing time + ~90 min",
    )
    spots: list[PlannedSpot] = Field(default_factory=list, description="Ordered spots; empty for pure transit days")
    hotel: str = Field(default="", description="Tonight's stay (name)")
    summary: str = Field(default="", description="One-sentence theme of the day, e.g. 'Arashiyama day'")


class OpenQuestion(BaseModel):
    question: str = Field(description="A decision only the traveller can make")
    options: list[str] = Field(description="2-4 concrete choices")


class TripPlan(BaseModel):
    destination: str = Field(description="Where this trip goes, display form")
    arrival_time: str = Field(
        default="",
        description="Landing time HH:MM if the traveller named one ('' otherwise)",
    )
    departure_time: str = Field(
        default="",
        description="Departure flight/train time HH:MM if named ('' otherwise)",
    )
    days: list[DayPlan] = Field(description="Day-by-day plan")
    total_usd: float = Field(default=0.0, description="Rough total budget in USD (trip_budget tool)")
    warnings: list[str] = Field(default_factory=list, description="Honest feasibility notes — never hide these")
    open_questions: list[OpenQuestion] = Field(
        default_factory=list, description="Decisions the agent needs from the traveller before this is final",
    )
