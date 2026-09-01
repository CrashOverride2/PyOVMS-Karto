import logging
from typing import List, Any
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

from .models import Trip
from .timestamps import as_utc

logger = logging.getLogger(__name__)

def _to_iso_z(dt):
    """Formats a datetime object to ISO 8601 with a 'Z' for Zulu/UTC.

    Converts rather than assuming. `timestamptz` comes back in the session's TimeZone,
    so on a PostgreSQL host set to anything but UTC this used to stamp a local wall
    clock with a Z — a GPX file that says an instant it is not. A naive value is a
    stored UTC timestamp whose label went missing and is taken as such.
    """
    if not dt:
        return ""
    return as_utc(dt).strftime('%Y-%m-%dT%H:%M:%SZ')

def generate_gpx_for_trip(trip: Trip, points: List[Any]) -> str:
    """
    Generates a GPX file content string for a given trip and its points.

    Args:
        trip: The Trip database object.
        points: A list of point data (e.g., SQLAlchemy Row objects)
                containing 'latitude', 'longitude', 'timestamp', and 'speed_kph'.

    Returns:
        A string containing the GPX data in XML format.
    """
    
    gpx = Element('gpx', {
        'version': '1.1',
        'creator': 'Karto Trip Service',
        'xmlns': 'http://www.topografix.com/GPX/1/1',
        'xmlns:xsi': 'http://www.w3.org/2001/XMLSchema-instance',
        'xsi:schemaLocation': 'http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd'
    })

    metadata = SubElement(gpx, 'metadata')
    SubElement(metadata, 'name').text = f"Trip for {trip.vehicle_id} on {trip.start_time.strftime('%Y-%m-%d')}"
    SubElement(metadata, 'time').text = _to_iso_z(trip.start_time)
    
    trk = SubElement(gpx, 'trk')
    SubElement(trk, 'name').text = f"Trip ID: {trip.id}"

    trkseg = SubElement(trk, 'trkseg')

    for point in points:
        trkpt = SubElement(trkseg, 'trkpt', {'lat': str(point.latitude), 'lon': str(point.longitude)})
        
        if point.altitude_m is not None:
            SubElement(trkpt, 'ele').text = f"{point.altitude_m:.1f}"
        
        SubElement(trkpt, 'time').text = _to_iso_z(point.timestamp)
        
        if point.speed_kph is not None:
            speed_ms = point.speed_kph / 3.6
            SubElement(trkpt, 'speed').text = f"{speed_ms:.2f}"

    rough_string = tostring(gpx, 'utf-8')
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ", encoding="utf-8").decode('utf-8')