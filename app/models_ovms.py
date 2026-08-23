from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Integer,
                        String)
from sqlalchemy.orm import declarative_base, relationship

OvmsBase = declarative_base()

class User(OvmsBase):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    
    is_totp_enabled = Column(Boolean, default=False, nullable=False)

    # Bumped by the OVMS server on password change/reset to invalidate every JWT
    # issued earlier. Mapped read-only here so this service can honour the same
    # revocation — the column is owned and migrated by the OVMS server.
    token_version = Column(Integer, nullable=False, default=0, server_default='0')

    vehicles = relationship("Vehicle", back_populates="owner")
    api_keys = relationship("ApiKey", back_populates="user")
class Vehicle(OvmsBase):
    __tablename__ = "vehicles"
    id = Column(Integer, primary_key=True)
    vehicle_id = Column(String(32), unique=True, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    enable_trip_tracking = Column(Boolean, default=False, nullable=False)
    # Read-only here, but essential: a vehicle id is free again once the vehicle is
    # deleted, so trips recorded before this row existed belong to a *previous*
    # registration and must not be served to whoever holds the id now.
    created_at = Column(DateTime(timezone=True), nullable=True)
    owner = relationship("User", back_populates="vehicles")
class ApiKey(OvmsBase):
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True)
    hashed_key = Column(String(128), nullable=False, unique=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    user = relationship("User", back_populates="api_keys")