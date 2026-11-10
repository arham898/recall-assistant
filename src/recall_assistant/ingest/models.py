"""Data models for NHTSA recall records."""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class RecallVehicle:
    """One make/model/year entry within a recall campaign."""

    make: str
    model: str
    year: int | None  # None when the source says 9999 (unknown)


@dataclass
class Recall:
    """A single NHTSA recall campaign, grouping all affected vehicles."""

    campaign_id: str
    manufacturer: str
    manufacturer_campaign_number: str
    component: str
    recall_type: str  # V=Vehicle, E=Equipment, C=Child seat, T=Tire
    potential_units_affected: int | None
    defect_summary: str
    consequence_summary: str
    corrective_action: str
    notes: str
    report_received_date: date | None
    record_creation_date: date | None
    manufacturing_begin_date: date | None
    manufacturing_end_date: date | None
    owner_notification_date: date | None
    influenced_by: str
    do_not_drive: bool
    park_outside: bool
    vehicles: list[RecallVehicle] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
