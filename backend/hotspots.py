"""
Deterministic civic hotspot detection (Milestone 20).

Pure, DB-free, and unit-testable in isolation -- mirrors backend/insights.py's
existing pattern of keeping "what does the data mean" logic completely
separate from database queries, so these rules can be tested without a
database and reused by anything that can supply a list of geo-tagged
issues.

Gemini is never involved here and never could be: hotspot MEMBERSHIP is
decided entirely by this deterministic geometry/counting logic. Gemini's
only role anywhere in this milestone (see backend/service.py's
get_civic_insights, which wraps already-detected hotspots as Insight
objects) is explaining an ALREADY-detected hotspot -- it is never asked
whether two points are "close," and never invents hotspot membership.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

# Complaints within this radius of each other are considered
# geographically "close" for hotspot purposes. A conservative
# neighborhood-scale radius -- tight enough that a hotspot represents a
# genuinely localized cluster, not an entire district.
HOTSPOT_RADIUS_KM = 0.5

# A hotspot must have at least this many member complaints -- two nearby
# complaints is routine, not a pattern worth surfacing.
HOTSPOT_MIN_COUNT = 3

# Only complaints created within this many days are considered for
# hotspot detection -- an old, possibly-already-resolved cluster from
# months ago isn't current operational intelligence.
HOTSPOT_TIME_WINDOW_DAYS = 30

_EARTH_RADIUS_KM = 6371.0


@dataclass(frozen=True)
class GeoIssuePoint:
    """One geo-tagged issue, as input to detect_hotspots(). Deliberately
    minimal and DB-free -- callers (backend/service.py) are responsible
    for translating an ORM Issue into this shape."""

    public_id: str
    latitude: float
    longitude: float
    category: str
    severity: str
    status: str
    created_at: datetime


@dataclass(frozen=True)
class Hotspot:
    """One detected geographic cluster of complaints."""

    center_latitude: float
    center_longitude: float
    complaint_count: int
    dominant_category: str
    category_breakdown: dict[str, int]
    severity_breakdown: dict[str, int]
    status_breakdown: dict[str, int]
    earliest_complaint_at: datetime
    latest_complaint_at: datetime
    priority_signal: str
    member_public_ids: list[str]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lng points, in kilometers.
    Standard haversine formula -- pure math, no external dependency."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _priority_signal(severity_breakdown: dict[str, int], complaint_count: int) -> str:
    """A cautious, deterministic attention signal -- never a fabricated
    precise score. HIGH if any Critical/High-severity complaint is
    present, or the cluster is unusually large; MEDIUM otherwise."""
    has_high_severity = bool(
        severity_breakdown.get("Critical", 0) or severity_breakdown.get("High", 0)
    )
    if has_high_severity or complaint_count >= 2 * HOTSPOT_MIN_COUNT:
        return "HIGH"
    return "MEDIUM"


def detect_hotspots(
    points: list[GeoIssuePoint],
    *,
    radius_km: float = HOTSPOT_RADIUS_KM,
    min_count: int = HOTSPOT_MIN_COUNT,
) -> list[Hotspot]:
    """Deterministic single-link geographic clustering.

    Iterates points in a STABLE, INPUT-ORDER-INDEPENDENT sequence (sorted
    by public_id) so the same input set always produces the same output
    regardless of how the caller's database query happened to order rows
    -- this is what "deterministic and testable" means in practice, not
    just "no randomness."

    Algorithm: for each not-yet-assigned point (in sorted order), start a
    new cluster and greedily absorb every other not-yet-assigned point
    within `radius_km` of ANY current cluster member (single-link/
    transitive closure, not just distance-to-seed) -- so a chain of
    points each within radius_km of a neighbor, even if the two ends are
    farther apart than radius_km from each other, still forms one
    cluster. Clusters below `min_count` are discarded entirely (not
    reported as tiny/low-confidence hotspots).

    Membership is decided ENTIRELY by geographic distance -- category is
    never a clustering criterion (a real-world hotspot may involve mixed
    complaint types), only reported afterward as descriptive context via
    category_breakdown/dominant_category.
    """
    ordered = sorted(points, key=lambda p: p.public_id)
    assigned: set[str] = set()
    hotspots: list[Hotspot] = []

    for seed in ordered:
        if seed.public_id in assigned:
            continue

        cluster = [seed]
        assigned.add(seed.public_id)
        # Transitive closure: keep expanding until a full pass finds no
        # new member -- deterministic because `ordered` (and therefore
        # iteration order) never changes.
        changed = True
        while changed:
            changed = False
            for candidate in ordered:
                if candidate.public_id in assigned:
                    continue
                if any(
                    haversine_km(
                        member.latitude, member.longitude,
                        candidate.latitude, candidate.longitude,
                    )
                    <= radius_km
                    for member in cluster
                ):
                    cluster.append(candidate)
                    assigned.add(candidate.public_id)
                    changed = True

        if len(cluster) < min_count:
            continue

        cluster.sort(key=lambda p: p.public_id)  # deterministic member order
        category_breakdown = dict(Counter(p.category for p in cluster))
        severity_breakdown = dict(Counter(p.severity for p in cluster))
        status_breakdown = dict(Counter(p.status for p in cluster))
        dominant_category = max(
            category_breakdown.items(), key=lambda item: (item[1], item[0])
        )[0]

        hotspots.append(
            Hotspot(
                center_latitude=sum(p.latitude for p in cluster) / len(cluster),
                center_longitude=sum(p.longitude for p in cluster) / len(cluster),
                complaint_count=len(cluster),
                dominant_category=dominant_category,
                category_breakdown=category_breakdown,
                severity_breakdown=severity_breakdown,
                status_breakdown=status_breakdown,
                earliest_complaint_at=min(p.created_at for p in cluster),
                latest_complaint_at=max(p.created_at for p in cluster),
                priority_signal=_priority_signal(severity_breakdown, len(cluster)),
                member_public_ids=[p.public_id for p in cluster],
            )
        )

    # Deterministic output order: largest cluster first, ties broken by
    # center coordinates for full reproducibility.
    hotspots.sort(key=lambda h: (-h.complaint_count, h.center_latitude, h.center_longitude))
    return hotspots
