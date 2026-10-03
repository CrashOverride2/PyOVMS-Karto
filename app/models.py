from uuid import uuid4

from geoalchemy2 import Geography
from sqlalchemy import (BigInteger, Column, DateTime, Float, ForeignKey, Index, Integer,
                        String, Uuid, func, text)
from sqlalchemy.orm import relationship

from .database import Base


class Trip(Base):
    __tablename__ = 'trips'
    id = Column(Uuid, primary_key=True, default=uuid4)
    vehicle_id = Column(String(32), nullable=False, index=True)
    status = Column(String(20), nullable=False)
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True))
    duration_seconds = Column(Integer)
    start_soc = Column(Float)
    end_soc = Column(Float)
    soc_used = Column(Float)
    energy_used_kwh = Column(Float)
    # Reading of v.b.energy.used when the trip started (or when it was first seen during
    # it). Persisted so a trip resumed after a restart keeps its energy baseline.
    start_energy_kwh = Column(Float)
    distance_km = Column(Float)
    average_speed_kph = Column(Float)
    map_preview_path = Column(String(255))
    map_preview_path_light = Column(String(255))
    start_location = Column(Geography('POINT', srid=4326))
    end_location = Column(Geography('POINT', srid=4326))

    gps_points = relationship("GPSPoint", back_populates="trip", cascade="all, delete-orphan", lazy="dynamic")

    __table_args__ = (
        Index('ix_trips_vehicle_status', 'vehicle_id', 'status'),
        # At most one open trip per vehicle. The tracker keeps to that with a lock per
        # vehicle, which only holds inside one process; the database holds it for all.
        Index('uq_trips_vehicle_in_progress', 'vehicle_id', unique=True,
              postgresql_where=text("status = 'in_progress'"),
              sqlite_where=text("status = 'in_progress'")),
    )


class GPSPoint(Base):
    __tablename__ = 'gps_points'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    trip_id = Column(Uuid, ForeignKey('trips.id', ondelete='CASCADE'), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    location = Column(Geography('POINT', srid=4326, spatial_index=True), nullable=False)
    speed_kph = Column(Float)
    altitude_m = Column(Float, nullable=True)

    trip = relationship("Trip", back_populates="gps_points")


class MapRegenerationQueue(Base):
    __tablename__ = 'map_regeneration_queue'
    trip_id = Column(Uuid, primary_key=True)
    status = Column(String(20), nullable=False, default='pending', index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class AuthFailureState(Base):
    __tablename__ = 'auth_failure_state'
    ip_address = Column(String(45), primary_key=True)
    failure_count = Column(Integer, nullable=False, default=0)
    window_started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_failure_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AuthIPBan(Base):
    __tablename__ = 'auth_ip_bans'
    ip_address = Column(String(45), primary_key=True)
    banned_until = Column(DateTime(timezone=True), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
