"""
The energy figure of a trip, from metrics whose meaning differs between the vehicle
integrations of the OVMS firmware. Karto has to work with every module out there, old
firmware included, so it takes neither metric at its word.

`v.b.energy.used` is "energy used on trip" — but when it resets is up to the integration:
on every ignition (smart EQ, Twizy), on a charge, never (Zoe Ph2's trip counter of the
instrument cluster), or with the module's boot. The tracker takes the difference over the
trip; what it gets is checked here against what a vehicle can use at all over the trip's
distance and duration (`plausible_energy_kwh()`), because a counter that changes its
source or its reset point mid-trip yields thousands of kWh.

`v.b.capacity` is defined as the usable capacity in kWh (metrics_standard.h), and some
integrations publish exactly that (VW e-Up, Toyota eTNGA, Volt/Ampera). Others publish
the energy *left* in the battery, which falls with the SoC: Renault Zoe Ph2, Kangoo 3 and
Twingo 3 Ph2, smart EQ, NIU GT EVO, VW e-Golf. Which of the two a vehicle sends is learned
from how the metric moves with the SoC (`classify_capacity()`) and only then used for the
SoC-based estimate (`soc_estimate_kwh()`); read as the capacity, the energy left makes
that estimate far too low.
"""

from typing import Optional

CAPACITY_TOTAL = "total"
CAPACITY_REMAINING = "remaining"

# Two capacity readings are only compared once the SoC has moved this far between them:
# over a few points the readings' own jitter and the polling lag of the capacity would
# decide, not the semantics.
CAPACITY_CLASSIFY_MIN_SOC_DELTA = 10.0
# The fitting reading must fit within this relative error, and the other must fit worse
# by the factor. The energy left is not quite proportional to the displayed SoC (the Zoe
# keeps a reserve below 0 %), hence the generous error; the margin keeps a pair that fits
# neither from deciding anything.
CAPACITY_CLASSIFY_MAX_ERROR = 0.2
CAPACITY_CLASSIFY_MARGIN = 2.0

# The most a trip can plausibly use: driving at a consumption no road vehicle reaches,
# plus heating or air conditioning at full power for the whole duration, plus the
# resolution of the counters. Generous on purpose — it is there to catch counters that
# jumped (thousands of kWh, or a cumulative counter read as the trip's), not to judge
# a heavy right foot.
MAX_KWH_PER_KM = 0.5
MAX_AUX_KW = 10.0
SLACK_KWH = 1.0

# The least: half of what even a scooter needs (the NIU GT EVO uses 0.04 kWh/km), less the
# counter's resolution (the Zoe Ph2's cluster counter counts in 0.1 kWh), so a short ride
# may still show 0.0. A counter that does not move — integrations that publish 0 without
# counting — would otherwise store a long ride as using nothing.
MIN_KWH_PER_KM = 0.02
COUNTER_RESOLUTION_KWH = 0.1


def energy_limit_kwh(distance_km: Optional[float], duration_seconds: Optional[float]) -> float:
    """The most energy a trip of this distance and duration can have used."""
    hours = max(duration_seconds or 0.0, 0.0) / 3600.0
    return max(distance_km or 0.0, 0.0) * MAX_KWH_PER_KM + hours * MAX_AUX_KW + SLACK_KWH


def energy_floor_kwh(distance_km: Optional[float]) -> float:
    """The least energy a trip of this distance can have used, as a counter shows it."""
    return max(distance_km or 0.0, 0.0) * MIN_KWH_PER_KM - COUNTER_RESOLUTION_KWH


def plausible_energy_kwh(kwh: Optional[float], distance_km: Optional[float],
                         duration_seconds: Optional[float]) -> Optional[float]:
    """`kwh` when a trip of this distance and duration can have used it, else None."""
    if kwh is None or kwh < 0:
        return None
    if kwh > energy_limit_kwh(distance_km, duration_seconds) or kwh < energy_floor_kwh(distance_km):
        return None
    return kwh


def classify_capacity(ref_soc: float, ref_kwh: float, soc: float, kwh: float) -> Optional[str]:
    """
    What two `v.b.capacity` readings at different SoCs say the metric is: CAPACITY_TOTAL
    when it stayed put while the SoC moved, CAPACITY_REMAINING when it moved with the SoC,
    None when the pair does not tell (too close, or fitting neither).
    """
    if abs(soc - ref_soc) < CAPACITY_CLASSIFY_MIN_SOC_DELTA:
        return None
    if min(ref_soc, soc, ref_kwh, kwh) <= 0:
        return None
    total_error = abs(kwh - ref_kwh) / max(kwh, ref_kwh)
    per_percent, ref_per_percent = kwh / soc, ref_kwh / ref_soc
    remaining_error = abs(per_percent - ref_per_percent) / max(per_percent, ref_per_percent)
    if total_error <= CAPACITY_CLASSIFY_MAX_ERROR and remaining_error >= CAPACITY_CLASSIFY_MARGIN * total_error:
        return CAPACITY_TOTAL
    if remaining_error <= CAPACITY_CLASSIFY_MAX_ERROR and total_error >= CAPACITY_CLASSIFY_MARGIN * remaining_error:
        return CAPACITY_REMAINING
    return None


def soc_estimate_kwh(capacity_kind: Optional[str], soc_used: Optional[float], end_soc: Optional[float],
                     start_capacity_kwh: Optional[float], end_capacity_kwh: Optional[float]) -> Optional[float]:
    """
    The energy a trip used, estimated from the SoC and `v.b.capacity`, for a trip whose
    counter gave no figure. None while the metric's meaning is not known: a guess at it
    is what produced the far too low figures.

    For the energy left, the drop over the trip is the battery's own account of what it
    gave and needs no SoC at all; without a reading from the trip's start, the energy per
    SoC point at its end stands in for the capacity.
    """
    if soc_used is None or soc_used <= 0 or end_capacity_kwh is None or end_capacity_kwh <= 0:
        return None
    if capacity_kind == CAPACITY_TOTAL:
        return round(soc_used / 100.0 * end_capacity_kwh, 3)
    if capacity_kind == CAPACITY_REMAINING:
        if start_capacity_kwh is not None and start_capacity_kwh > end_capacity_kwh:
            return round(start_capacity_kwh - end_capacity_kwh, 3)
        if end_soc is not None and end_soc > 0:
            return round(soc_used * end_capacity_kwh / end_soc, 3)
    return None
