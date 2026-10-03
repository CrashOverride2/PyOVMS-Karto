from datetime import date
from typing import List, Literal, Optional, Any
from uuid import UUID

from pydantic import BaseModel, Field, computed_field

from .timestamps import UtcDatetime
class PaginationDetails(BaseModel):
    total_items: int
    total_pages: int
    current_page: int
    limit: int
class TripSummary(BaseModel):
    id: UUID
    vehicle_id: str
    status: str
    start_time: UtcDatetime
    end_time: Optional[UtcDatetime] = None
    duration_seconds: Optional[int] = None
    distance_km: Optional[float] = None
    soc_used: Optional[float] = None
    energy_used_kwh: Optional[float] = None
    average_speed_kph: Optional[float] = None
    map_preview_path: Optional[str] = None
    map_preview_path_light: Optional[str] = None
    start_soc: Optional[float] = None
    end_soc: Optional[float] = None

    @computed_field
    @property
    def consumption_kwh_per_100km(self) -> Optional[float]:
        if self.energy_used_kwh and self.distance_km and self.distance_km > 0:
            return round((self.energy_used_kwh / self.distance_km) * 100, 1)
        return None

    @computed_field
    @property
    def consumption_kwh_per_100mi(self) -> Optional[float]:
        if self.consumption_kwh_per_100km is not None:
            return round(self.consumption_kwh_per_100km * 1.60934, 1)
        return None

    @computed_field
    @property
    def distance_miles(self) -> Optional[float]:
        if self.distance_km is not None:
            return round(self.distance_km * 0.621371, 2)
        return None

    @computed_field
    @property
    def average_speed_mph(self) -> Optional[float]:
        if self.average_speed_kph is not None:
            return round(self.average_speed_kph * 0.621371, 1)
        return None

    class Config:
        from_attributes = True
class TripDetail(TripSummary):
    geojson: Optional[Any] = Field(None, description="A GeoJSON LineString object representing the trip track.")

    class Config:
        from_attributes = True
class OpenTrip(BaseModel):
    """
    A trip that is still running: what has been recorded of it so far. Nothing here is
    final — the trip may yet be discarded as too short when it ends.
    """
    id: UUID
    vehicle_id: str
    status: str = Field(..., description="Always `in_progress`.")
    start_time: UtcDatetime
    start_soc: Optional[float] = None
    start_lat: Optional[float] = None
    start_lon: Optional[float] = None
    last_point_time: Optional[UtcDatetime] = Field(None, description="Time of the newest recorded point.")
    last_lat: Optional[float] = None
    last_lon: Optional[float] = None
    last_speed_kph: Optional[float] = None
    duration_seconds: Optional[int] = Field(None, description="From the start to the newest point.")
    distance_km: Optional[float] = Field(None, description="Length of the track recorded so far.")
    average_speed_kph: Optional[float] = None
    phase: Optional[Literal["driving", "ending"]] = Field(
        None,
        description="`driving`, or `ending` while the vehicle is off and the trip waits out its end "
                    "grace period. Null while the service has not picked the trip up since a restart.",
    )
    current_soc: Optional[float] = Field(None, description="The vehicle's SoC now, when known.")
    energy_used_kwh: Optional[float] = Field(None, description="Energy used so far, when known.")

    @computed_field
    @property
    def soc_used(self) -> Optional[float]:
        if self.start_soc is not None and self.current_soc is not None:
            return round(self.start_soc - self.current_soc, 1)
        return None

    @computed_field
    @property
    def distance_miles(self) -> Optional[float]:
        if self.distance_km is not None:
            return round(self.distance_km * 0.621371, 2)
        return None

    @computed_field
    @property
    def average_speed_mph(self) -> Optional[float]:
        if self.average_speed_kph is not None:
            return round(self.average_speed_kph * 0.621371, 1)
        return None


class PaginatedTripSummary(BaseModel):
    """
    A model to hold a list of trip summaries along with pagination metadata.
    """
    pagination: PaginationDetails
    trips: List[TripSummary]
class StatDetail(BaseModel):
    """
    Represents aggregated statistics for a single time period.
    """
    period: date = Field(..., description="The start date of the aggregation period (day, week, or month).")
    total_distance_km: float
    total_duration_seconds: int
    trip_count: int
    average_distance_per_trip_km: float
    average_duration_per_trip_seconds: int
    overall_average_speed_kph: float
    longest_trip_km: float
    shortest_trip_km: float
    most_active_day_of_week: Optional[str] = None
    total_soc_used: Optional[float] = None
    total_energy_used_kwh: Optional[float] = None
class TotalStats(BaseModel):
    """
    Represents the lifetime total statistics for a vehicle.
    """
    total_distance_km: float
    total_duration_seconds: int
    total_trips: int
    average_distance_per_trip_km: float
    average_duration_per_trip_seconds: int
    overall_average_speed_kph: float
    longest_trip_km: float
    shortest_trip_km: float
    most_active_day_of_week: Optional[str] = None
    total_soc_used: Optional[float] = None
    total_energy_used_kwh: Optional[float] = None
class TripStatistics(BaseModel):
    """
    Holds aggregated trip statistics for daily, weekly, and monthly periods, plus totals.
    """
    total: TotalStats
    daily: List[StatDetail]
    weekly: List[StatDetail]
    monthly: List[StatDetail]

class HeatmapPoint(BaseModel):
    """Represents a single point in a heatmap, with a weight."""
    lat: float
    lon: float
    weight: int

class TripSearchResult(TripSummary):
    """
    Represents a trip found in a proximity search, including its distance to
    the search origin. The distance is only included for proximity searches.
    """
    distance_m: Optional[float] = None

    class Config:
        from_attributes = True

class PaginatedTripSearchSummary(BaseModel):
    """
    Holds a list of trip search results along with pagination metadata.
    """
    pagination: PaginationDetails
    trips: List[TripSearchResult]