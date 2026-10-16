"""Component group mappings for the golden evaluation set.

Each group maps a human-readable name to a tuple of exact top-level
component categories (TRIM(SPLIT_PART(component, ':', 1))).  Matching
is always by membership in the tuple, never by prefix.
"""

from __future__ import annotations

from typing import NamedTuple


class ExcludedSub(NamedTuple):
    """A sub-component to exclude from group matching.

    Matched against TRIM(SPLIT_PART(component, ':', 2)) and
    TRIM(SPLIT_PART(component, ':', 3)).  level3 empty string
    means any third level is excluded.
    """

    level2: str
    level3: str = ""


class ComponentGroup(NamedTuple):
    """One named component group."""

    key: str
    plain_name: str
    modifier: str
    noun_phrase: str
    categories: tuple[str, ...]
    excluded_subs: tuple[ExcludedSub, ...] = ()


COMPONENT_GROUPS: tuple[ComponentGroup, ...] = (
    ComponentGroup(
        "brakes",
        "brakes",
        "brake",
        "the brakes",
        (
            "SERVICE BRAKES, HYDRAULIC",
            "SERVICE BRAKES, AIR",
            "SERVICE BRAKES, ELECTRIC",
            "SERVICE BRAKES",
            "PARKING BRAKE",
            "SERVICE BRAKES, HYDRAULIC; AUTOHOLD BRAKE SYSTEM/BRAKE HOLD",
        ),
    ),
    ComponentGroup(
        "fuel_system",
        "fuel system",
        "fuel system",
        "the fuel system",
        (
            "FUEL SYSTEM, GASOLINE",
            "FUEL SYSTEM, DIESEL",
            "FUEL SYSTEM, OTHER",
            "FUEL SYSTEM",
            "FUEL/PROPULSION SYSTEM",
        ),
    ),
    ComponentGroup(
        "engine",
        "engine",
        "engine",
        "the engine",
        ("ENGINE", "ENGINE AND ENGINE COOLING"),
    ),
    ComponentGroup(
        "collision_avoidance",
        "collision avoidance",
        "collision avoidance",
        "the collision avoidance system",
        ("FORWARD COLLISION AVOIDANCE",),
    ),
    ComponentGroup(
        "stability_control",
        "stability control",
        "stability control",
        "the stability control system",
        ("ELECTRONIC STABILITY CONTROL (ESC)",),
    ),
    ComponentGroup(
        "airbags",
        "airbags",
        "airbag",
        "the airbags",
        ("AIR BAGS",),
    ),
    ComponentGroup(
        "seat_belts",
        "seat belts",
        "seat belt",
        "the seat belts",
        ("SEAT BELTS",),
    ),
    ComponentGroup(
        "steering",
        "steering",
        "steering",
        "the steering",
        ("STEERING",),
    ),
    ComponentGroup(
        "exterior_lights",
        "exterior lights",
        "exterior lighting",
        "the exterior lights",
        ("EXTERIOR LIGHTING",),
    ),
    ComponentGroup(
        "tires",
        "tires",
        "tire",
        "the tires",
        ("TIRES",),
    ),
    ComponentGroup(
        "suspension",
        "suspension",
        "suspension",
        "the suspension",
        ("SUSPENSION",),
    ),
    ComponentGroup(
        "powertrain",
        "powertrain",
        "powertrain",
        "the powertrain",
        ("POWER TRAIN",),
    ),
    ComponentGroup(
        "backup_camera",
        "backup camera",
        "backup camera",
        "the backup camera",
        ("BACK OVER PREVENTION",),
        (
            ExcludedSub("WARNINGS", "EXTERNAL/PEDESTRIAN ALERT"),
            ExcludedSub("AUTOMATIC SYSTEM BRAKING"),
        ),
    ),
    ComponentGroup(
        "seats",
        "seats",
        "seat",
        "the seats",
        ("SEATS",),
    ),
    ComponentGroup(
        "electrical_system",
        "electrical system",
        "electrical system",
        "the electrical system",
        ("ELECTRICAL SYSTEM",),
    ),
    ComponentGroup(
        "wheels",
        "wheels",
        "wheel",
        "the wheels",
        ("WHEELS",),
    ),
)

# Fast lookup: top-level category string -> ComponentGroup
CATEGORY_TO_GROUP: dict[str, ComponentGroup] = {
    cat: grp for grp in COMPONENT_GROUPS for cat in grp.categories
}
