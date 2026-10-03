"""Doctor availability and appointment-slot schemas."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.auth import trim_strings
from app.services.scheduling import SLOT_CHOICES, WEEKDAYS


class AvailabilityRule(BaseModel):
    weekday: int = Field(ge=0, le=6)  # 0 = Monday
    start_time: time
    end_time: time
    slot_minutes: int

    @field_validator("start_time", "end_time")
    @classmethod
    def whole_minutes(cls, v: time) -> time:
        return v.replace(second=0, microsecond=0, tzinfo=None)

    @field_validator("slot_minutes")
    @classmethod
    def known_length(cls, v: int) -> int:
        if v not in SLOT_CHOICES:
            raise ValueError(
                f"Slot length must be one of {', '.join(map(str, SLOT_CHOICES))} minutes"
            )
        return v

    @model_validator(mode="after")
    def window_fits_a_slot(self) -> "AvailabilityRule":
        start = datetime.combine(date.min, self.start_time)
        end = datetime.combine(date.min, self.end_time)
        if end <= start:
            raise ValueError(f"{WEEKDAYS[self.weekday]}: end time must be after start time")
        if end - start < timedelta(minutes=self.slot_minutes):
            raise ValueError(f"{WEEKDAYS[self.weekday]}: the window is shorter than one slot")
        return self


class AvailabilityUpdate(BaseModel):
    rules: list[AvailabilityRule] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def no_overlaps(self) -> "AvailabilityUpdate":
        by_day: dict[int, list[AvailabilityRule]] = {}
        for rule in self.rules:
            by_day.setdefault(rule.weekday, []).append(rule)
        for weekday, rules in by_day.items():
            rules.sort(key=lambda r: r.start_time)
            for earlier, later in zip(rules, rules[1:]):
                if later.start_time < earlier.end_time:
                    raise ValueError(f"{WEEKDAYS[weekday]}: hours overlap")
        return self


class TimeOffCreate(BaseModel):
    _trim = model_validator(mode="before")(classmethod(lambda cls, v: trim_strings(v)))

    start_date: date
    end_date: date
    reason: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def sensible_range(self) -> "TimeOffCreate":
        if self.end_date < self.start_date:
            raise ValueError("end_date: must be on or after the start date")
        if (self.end_date - self.start_date).days > 365:
            raise ValueError("end_date: time off can cover at most one year")
        return self
