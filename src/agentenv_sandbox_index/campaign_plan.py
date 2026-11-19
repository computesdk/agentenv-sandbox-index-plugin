"""Campaign inputs and UTC window selection; no runner dependencies."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from . import methodology

HOUR = 3600
LEAD_MINUTES = 15
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def epoch(value: str) -> int:
    return int(datetime.strptime(value, UTC_FORMAT).replace(tzinfo=UTC).timestamp())


def iso(value: int) -> str:
    return datetime.fromtimestamp(value, UTC).strftime(UTC_FORMAT)


@dataclass(frozen=True)
class Plan:
    id: str
    start: str
    hours: int
    providers: list[str]
    revision: str

    @classmethod
    def parse(cls, raw: str) -> "Plan":
        plan = cls(**json.loads(raw))
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", plan.id):
            raise ValueError("Campaign ID must be a lowercase slug of at most 64 characters")
        if not re.fullmatch(r"[a-f0-9]{40}", plan.revision):
            raise ValueError("Campaign revision must be a full commit SHA")
        if type(plan.hours) is not int or not 1 <= plan.hours <= 168 or epoch(plan.start) % HOUR:
            raise ValueError("Campaign must start at a UTC hour and last 1–168 whole hours")
        if (
            not isinstance(plan.providers, list)
            or not plan.providers
            or any(name not in methodology.PROVIDERS for name in plan.providers)
            or len(set(plan.providers)) != len(plan.providers)
        ):
            raise ValueError("Campaign must list unique hosted providers")
        return plan

    def contains(self, window: int) -> bool:
        start = epoch(self.start)
        return window % HOUR == 0 and start <= window < start + self.hours * HOUR


def scheduled_window(fire: str) -> int:
    due = datetime.fromisoformat(fire)
    if due.utcoffset() != UTC.utcoffset(None) or due.minute != 60 - LEAD_MINUTES or due.second or due.microsecond:
        raise ValueError("Use the ComputeSDK schedule's :45 UTC due timestamp")
    return int(due.timestamp()) + LEAD_MINUTES * 60
