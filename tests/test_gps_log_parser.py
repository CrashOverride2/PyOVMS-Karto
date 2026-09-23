import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Provide the mandatory settings before app.config is imported, so the tests run
# without a karto.env/.env file (e.g. in CI).
os.environ.setdefault("SECRET_KEY_JWT", "test")
os.environ.setdefault("ALGORITHM", "HS256")
os.environ.setdefault("MQTT_BROKER_HOST", "localhost")
os.environ.setdefault("MQTT_USER", "test")
os.environ.setdefault("MQTT_PASSWORD", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/karto_test")
os.environ.setdefault("OVMS_DATABASE_URL", "postgresql://localhost/karto_test")

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from app.trip_tracker import (GPS_LOG_PARSERS, parse_rt_gps_log, parse_xne_gps_log, parse_xsq_gps_log,
                              trip_tracker_service)


def _utc_now() -> int:
    return int(time.time())


# --- XNE-GPS-Log (NIU GT EVO) ------------------------------------------------

def test_xne_parse_full_record():
    ts = _utc_now() - 600
    payload = f"XNE-GPS-Log,{ts},86400,49.323456,8.201234,43.2,187,1.1,9,45.0,12345.6,87.3"
    point = parse_xne_gps_log(payload)
    assert point is not None
    assert point.timestamp == datetime.fromtimestamp(ts, tz=timezone.utc)
    assert point.lat == 49.323456
    assert point.lon == 8.201234
    assert point.speed_kph == 45.0  # vehicle speed (field 9), not the GPS speed
    assert point.soc == 87.3
    assert point.altitude_m is None  # the NIU CAN bus carries no altitude


def test_xne_parse_soc_zero_is_no_reading():
    """The module writes 0 while no battery is detected."""
    ts = _utc_now() - 60
    point = parse_xne_gps_log(f"XNE-GPS-Log,{ts},86400,49.3,8.2,43.2,187,1.1,9,45.0,12345.6,0.0")
    assert point is not None
    assert point.soc is None


def test_xne_parse_minimal_record_without_optional_fields():
    ts = _utc_now() - 60
    point = parse_xne_gps_log(f"XNE-GPS-Log,{ts},86400,49.3,8.2")
    assert point is not None
    assert (point.lat, point.lon) == (49.3, 8.2)
    assert point.speed_kph is None
    assert point.soc is None


def test_xne_parse_speed_falls_back_to_gps_speed():
    ts = _utc_now() - 60
    # Fields up to GPS speed (index 5) present, vehicle speed (index 9) missing
    point = parse_xne_gps_log(f"XNE-GPS-Log,{ts},86400,49.3,8.2,43.2")
    assert point is not None
    assert point.speed_kph == 43.2


def test_xne_parse_rejects_out_of_range_coordinates():
    ts = _utc_now()
    assert parse_xne_gps_log(f"XNE-GPS-Log,{ts},86400,91.0,8.2") is None
    assert parse_xne_gps_log(f"XNE-GPS-Log,{ts},86400,49.3,181.0") is None
    assert parse_xne_gps_log(f"XNE-GPS-Log,{ts},86400,0.000000,0.000000") is None


def test_xne_parse_rejects_unsynced_clock():
    # Module clock not NTP/GSM synced yet (epoch near zero)
    assert parse_xne_gps_log("XNE-GPS-Log,12345,86400,49.3,8.2") is None
    # Timestamp from the future
    future = _utc_now() + 3600
    assert parse_xne_gps_log(f"XNE-GPS-Log,{future},86400,49.3,8.2") is None


def test_xne_parse_rejects_garbage():
    assert parse_xne_gps_log("") is None
    assert parse_xne_gps_log("XNE-GPS-Log") is None
    assert parse_xne_gps_log("XNE-GPS-Log,abc,86400,49.3,8.2") is None
    assert parse_xne_gps_log(f"XNE-GPS-Log,{_utc_now()},86400,notanumber,8.2") is None


# --- RT-GPS-Log (Renault Twizy) ----------------------------------------------

def test_rt_parse_full_record():
    # RT-GPS-Log,<odometer>,<expiry>,<lat>,<lon>,<alt>,<direction>,<speed>,<gpslock>,...
    payload = "RT-GPS-Log,123456,86400,51.301234,6.501234,42,265,38,1,0,31,352,120,15,780,80,192,3520,-500,1.000,1.000,0,0"
    point = parse_rt_gps_log(payload)
    assert point is not None
    assert point.timestamp is None  # RT records carry no time, topic age is used instead
    assert point.lat == 51.301234
    assert point.lon == 6.501234
    assert point.altitude_m == 42.0
    assert point.speed_kph == 38.0
    assert point.soc is None


def test_rt_parse_rejects_record_without_gps_lock():
    payload = "RT-GPS-Log,123456,86400,51.301234,6.501234,42,265,38,0,0,31"
    assert parse_rt_gps_log(payload) is None


def test_rt_parse_rejects_short_or_invalid_records():
    assert parse_rt_gps_log("RT-GPS-Log,123456,86400,51.3,6.5") is None  # too short
    assert parse_rt_gps_log("RT-GPS-Log,123456,86400,91.0,6.5,42,265,38,1") is None
    assert parse_rt_gps_log("RT-GPS-Log,123456,86400,0,0,42,265,38,1") is None


# --- XSQ-GPS-Log (smart EQ) -------------------------------------------------

def test_xsq_parse_full_record():
    # Layout as written by OvmsVehicleSmartEQ::SendGPSLog()
    payload = "XSQ-GPS-Log,123456,86400,49.321234,8.401234,112,87,52,1,0,-71,12.5,1.234,0.321,35.2"
    point = parse_xsq_gps_log(payload)
    assert point is not None
    assert point.timestamp is None  # no time of its own, topic age is used instead
    assert point.lat == 49.321234
    assert point.lon == 8.401234
    assert point.altitude_m == 112.0
    assert point.speed_kph == 52.0
    assert point.soc is None
    assert point.fix_age_s == 0.0


def test_xsq_parse_reads_the_position_age():
    payload = "XSQ-GPS-Log,123456,86400,49.321234,8.401234,112,87,52,1,17,-71,12.5,1.234,0.321,35.2"
    assert parse_xsq_gps_log(payload).fix_age_s == 17.0


def test_rt_parse_has_no_position_age():
    payload = "RT-GPS-Log,123456,86400,51.301234,6.501234,42,265,38,1,99,31"
    assert parse_rt_gps_log(payload).fix_age_s is None


def test_xsq_parse_rejects_record_without_gps_lock():
    payload = "XSQ-GPS-Log,123456,86400,49.321234,8.401234,112,87,52,0,0,-71,12.5,1.234,0.321,35.2"
    assert parse_xsq_gps_log(payload) is None


def test_xsq_parse_rejects_short_or_invalid_records():
    assert parse_xsq_gps_log("XSQ-GPS-Log,123456,86400,49.3,8.4") is None  # too short
    assert parse_xsq_gps_log("XSQ-GPS-Log,123456,86400,49.3,181.0,112,87,52,1") is None
    assert parse_xsq_gps_log("XSQ-GPS-Log,123456,86400,0.000000,0.000000,112,87,52,1") is None


# --- Dispatcher --------------------------------------------------------------

def test_dispatcher_knows_all_formats():
    assert GPS_LOG_PARSERS["XNE-GPS-Log"] is parse_xne_gps_log
    assert GPS_LOG_PARSERS["RT-GPS-Log"] is parse_rt_gps_log
    assert GPS_LOG_PARSERS["XSQ-GPS-Log"] is parse_xsq_gps_log


def test_dispatcher_ignores_unknown_record_types():
    # RT-PWR-Log and other data notifications must be ignored, not raise
    assert trip_tracker_service.process_data_notification("TEST", "RT-PWR-Log,1,2,3", 0) is None
    assert trip_tracker_service.process_data_notification("TEST", "", 0) is None


def test_dispatcher_returns_coroutine_for_known_type():
    ts = _utc_now() - 60
    coro = trip_tracker_service.process_data_notification("TEST", f"XNE-GPS-Log,{ts},86400,49.3,8.2", 60)
    assert coro is not None
    coro.close()  # not awaited in this unit test
