import logging
from typing import List, Any
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

from .models import Trip

logger = logging.getLogger(__name__)

def generate_kml_for_trip(trip: Trip, points: List[Any]) -> str:
    """
    Generates a KML file content string for a given trip and its points.

    Args:
        trip: The Trip database object.
        points: A list of point data (e.g., SQLAlchemy Row objects)
                containing 'latitude', 'longitude', and 'altitude_m'.

    Returns:
        A string containing the KML data in XML format.
    """

    kml = Element('kml', {'xmlns': 'http://www.opengis.net/kml/2.2'})
    doc = SubElement(kml, 'Document')
    
    style = SubElement(doc, 'Style', {'id': 'tripLineStyle'})
    line_style = SubElement(style, 'LineStyle')
    SubElement(line_style, 'color').text = 'ff0000ff'
    SubElement(line_style, 'width').text = '4'

    placemark = SubElement(doc, 'Placemark')
    SubElement(placemark, 'name').text = f"Trip for {trip.vehicle_id}"
    description_text = (
        f"Trip ID: {trip.id}\n"
        f"Started: {trip.start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        f"Ended: {trip.end_time.strftime('%Y-%m-%d %H:%M:%S UTC') if trip.end_time else 'N/A'}"
    )
    SubElement(placemark, 'description').text = description_text
    SubElement(placemark, 'styleUrl').text = '#tripLineStyle'

    linestring = SubElement(placemark, 'LineString')
    SubElement(linestring, 'tessellate').text = '1'
    
    coordinates_list = []
    for point in points:
        altitude = point.altitude_m if point.altitude_m is not None else 0
        coordinates_list.append(f"{point.longitude},{point.latitude},{altitude}")
    
    SubElement(linestring, 'coordinates').text = " ".join(coordinates_list)
    
    rough_string = tostring(kml, 'utf-8')
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ", encoding="utf-8").decode('utf-8')