from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    LOG_LEVEL: str = "INFO"

    JWT_PUBLIC_KEY: Optional[str] = None

    # Retained only so an existing karto.env that still sets them does not fail to load.
    # Neither is used any more; drop them from your config file.
    SECRET_KEY_JWT: Optional[str] = None
    ALGORITHM: Optional[str] = None

    MQTT_BROKER_HOST: str
    MQTT_BROKER_PORT: int = 1883
    MQTT_USER: str
    MQTT_PASSWORD: str

    DATABASE_URL: str
    OVMS_DATABASE_URL: str

    KARTO_PMTILES_PATH: Optional[Path] = None
    MAPS_STORAGE_PATH: Path = Path("/data/maps")

    KARTO_TILE_URL_TEMPLATE: Optional[str] = None
    KARTO_ALLOW_EXTERNAL_TILES: bool = False

    KARTO_TRIP_END_GRACE_PERIOD_SECONDS: int = 120
    KARTO_TRIP_TIMEOUT_SECONDS: int = 7200
    
    KARTO_GPS_MIN_SPEED_KPH: float = 5.0
    KARTO_GPS_MIN_DISTANCE_METERS: float = 20.0
    KARTO_GPS_UPDATE_MIN_INTERVAL_SECONDS: float = 1.0

    KARTO_GPS_BATCH_DEBOUNCE_SECONDS: float = 0.4
    KARTO_GPS_BATCH_MAX_WINDOW_SECONDS: float = 2.0
    KARTO_GPS_PAIR_MAX_SKEW_SECONDS: float = 3.0

    KARTO_TRIP_MIN_DISTANCE_KM: float = 1.0

    KARTO_GPSLOG_BACKDATE_MAX_SECONDS: int = 3600
    KARTO_GPSLOG_COMPLETED_ATTACH_SLACK_SECONDS: int = 900
    KARTO_GPSLOG_DEDUPE_SECONDS: float = 4.0
    KARTO_GPSLOG_FILTER_RESET_SECONDS: float = 120.0
    KARTO_GPSLOG_MAX_FIX_AGE_SECONDS: float = 10.0

    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT_SECONDS: int = 30

    MQTT_MAX_QUEUED_MESSAGES: int = 2000
    MQTT_WORKER_COUNT: int = 4

    API_FAIL_LIMIT: int = 10
    API_FAIL_WINDOW_SECONDS: int = 60
    API_BAN_MINUTES: int = 60

    SERVER_HOST: str = "127.0.0.1"
    HTTP_PORT: int = 8001

    ENABLE_API_DOCS: bool = False

    FORWARDED_ALLOW_IPS: str = "127.0.0.1"


    class Config:
        env_file_encoding = 'utf-8'
        extra = 'ignore'

        project_root = Path(__file__).resolve().parent.parent
        env_file: tuple[Path, ...] = (
            project_root / "karto.env",
            project_root / ".env",
        )

settings = Settings()