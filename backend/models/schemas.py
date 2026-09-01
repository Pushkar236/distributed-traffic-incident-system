"""Request bodies accepted by the REST API (validated by FastAPI / pydantic)."""
from typing import Literal

from pydantic import BaseModel, Field


class IncidentCreate(BaseModel):
    type: Literal["Accident", "Road Blockage", "Vehicle Breakdown",
                  "Heavy Congestion", "Traffic Signal Failure"]
    location: str = Field(min_length=2, max_length=120)
    severity: Literal["High", "Medium", "Low"]
    description: str = Field("", max_length=400)
    detected_by: Literal["PUNE-TR-01", "MUM-TR-01", "NSK-TR-01"]


class IncidentStatusPatch(BaseModel):
    status: Literal["RESOLVED", "MONITORING", "REPORTED"]


class EventIn(BaseModel):
    """A message published to the broker by a node process."""
    type: str
    source: str
    target: str = ""
    channel: str = "BROKER"
    summary: str = ""
    payload: dict = {}
