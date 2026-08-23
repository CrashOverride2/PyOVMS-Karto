import argparse
import json
import sys

import requests

def parse_args():
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(description="Karto API Client Simulator")
    parser.add_argument("--host", default="http://localhost:8001", help="The base URL of the Karto service")
    parser.add_argument("--vehicle-id", required=True, help="The vehicle ID to query for trips")
    parser.add_argument("--api-key", required=True, help="The full API key for authentication (from the OVMS profile page)")
    parser.add_argument("--page", type=int, default=1, help="The page number to request for paginated results")
    parser.add_argument("--limit", type=int, default=6, help="The number of items per page")
    parser.add_argument("--start-date", help="Start date for stats (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="End date for stats (YYYY-MM-DD)")
    parser.add_argument("--daily-limit", type=int, default=14, help="The number of daily items for stats")
    return parser.parse_args()


def print_trip_summary(trip: dict):
    """Prints a formatted summary of a single trip."""
    print(f"  - Trip ID: {trip.get('id')}")
    print(f"    Status: {trip.get('status')}")
    print(f"    Started: {trip.get('start_time')}")
    print(f"    Ended:   {trip.get('end_time')}")
    print(f"    Distance: {trip.get('distance_km'):.2f} km" if trip.get('distance_km') is not None else "    Distance: N/A")
    print(f"    Map: {trip.get('map_preview_path')}")


def print_stats_summary(stats: dict):
    """Prints a formatted summary of trip statistics."""
    print("\n--- Trip Statistics Summary ---")
    
    total = stats.get("total", {})
    if total and total.get('total_trips', 0) > 0:
        print("\nLifetime Totals (for selected period):")
        print(f"  Total Trips:    {total.get('total_trips', 0)}")
        print(f"  Total Distance: {total.get('total_distance_km', 0):.2f} km")
        print(f"  Total Duration: {total.get('total_duration_seconds', 0) / 3600:.2f} hours")
        print(f"  Avg Speed:      {total.get('overall_average_speed_kph', 0):.1f} kph")
        print(f"  Avg Distance:   {total.get('average_distance_per_trip_km', 0):.2f} km / trip")
        print(f"  Avg Duration:   {total.get('average_duration_per_trip_seconds', 0) / 60:.1f} min / trip")
        print(f"  Longest Trip:   {total.get('longest_trip_km', 0):.2f} km")
        print(f"  Shortest Trip:  {total.get('shortest_trip_km', 0):.2f} km")
        if total.get('most_active_day_of_week'):
            print(f"  Most Active Day:{total.get('most_active_day_of_week')}")
    elif total:
        print("\nLifetime Totals: No valid trips recorded in this period.")

    periods = [("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly")]
    for key, title in periods:
        period_data = stats.get(key)
        if period_data:
            print(f"\n{title} Stats (most recent first):")
            for item in period_data: # Removed slicing to show full API response
                print(f"  - Period Start: {item.get('period')}")
                print(f"    Stats:      {item.get('trip_count', 0)} trips, {item.get('total_distance_km', 0):.2f} km total, {item.get('overall_average_speed_kph', 0):.1f} kph avg")
                print(f"    Trip Range: {item.get('shortest_trip_km', 0):.2f} km (shortest) to {item.get('longest_trip_km', 0):.2f} km (longest)")
                if item.get('most_active_day_of_week') and key != 'daily':
                    print(f"    Busiest Day:{item.get('most_active_day_of_week')}")
        else:
             print(f"\nNo {title.lower()} stats available for this period.")
    print("-" * 40)

def run_api_tests(base_url: str, vehicle_id: str, api_key: str, page: int, limit: int, start_date: str, end_date: str, daily_limit: int):
    """Fetches and displays trip and stats data from the Karto API using an API key."""
    
    headers = {
        "X-API-Key": api_key,
    }
    
    print("\n--- Karto API Client (API Key Auth) ---")
    print(f"Querying for Vehicle ID: {vehicle_id.upper()}")
    print(f"Requesting Page: {page}, Limit: {limit}")
    print(f"Stats Range: {start_date or 'Beginning'} to {end_date or 'Today'} with daily limit {daily_limit}")
    print("-" * 40)
    
    try:
        list_url = f"{base_url}/api/karto/v1/vehicles/{vehicle_id}/trips"
        params = {"page": page, "limit": limit}
        print(f"\n Fetching trip list from: {list_url} with params: {params}")
        
        response = requests.get(list_url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        
        response_data = response.json()
        trips = response_data.get("trips", [])
        pagination = response_data.get("pagination", {})
        
        if not trips:
            print("\nResult: No trips found on this page.")
            if pagination:
                print(f"Pagination Info: Page {pagination.get('current_page')} of {pagination.get('total_pages')}. Total Trips: {pagination.get('total_items')}")
        else:
            print(f"\nSuccess! Found {len(trips)} trip(s) on this page.")
            if pagination:
                 print(f"Pagination Info: Page {pagination.get('current_page')} of {pagination.get('total_pages')}. Total Trips: {pagination.get('total_items')}")

            for trip in trips:
                print_trip_summary(trip)

            first_trip_id = trips[0].get('id')
            if not first_trip_id:
                print("\nCould not determine the first trip ID on this page.")
            else:
                detail_url = f"{base_url}/api/karto/v1/trips/{first_trip_id}"
                print(f"\n Fetching full details for first trip on page ({first_trip_id})...")
                
                detail_response = requests.get(detail_url, headers=headers, timeout=10)
                detail_response.raise_for_status()
                
                trip_details = detail_response.json()
                
                print("\n--- First Trip on Page Details ---")
                for key, value in trip_details.items():
                    if key != "geojson":
                        print(f"  {key.replace('_', ' ').title()}: {value}")
                
                if "geojson" in trip_details and trip_details["geojson"]:
                    geojson_data = json.loads(trip_details["geojson"])
                    coords = geojson_data.get("coordinates", [])
                    print(f"  GeoJSON: Found a LineString with {len(coords)} coordinates.")
                else:
                    print("  GeoJSON: Not available.")

        stats_url = f"{base_url}/api/karto/v1/vehicles/{vehicle_id}/stats"
        stats_params = {}
        if start_date:
            stats_params["start_date"] = start_date
        if end_date:
            stats_params["end_date"] = end_date
        if daily_limit:
            stats_params["daily_limit"] = daily_limit

        print(f"\n Fetching trip statistics from: {stats_url} with params: {stats_params}")

        stats_response = requests.get(stats_url, headers=headers, params=stats_params, timeout=10)
        stats_response.raise_for_status()

        stats_data = stats_response.json()
        print_stats_summary(stats_data)

    except requests.exceptions.RequestException as e:
        print("\nERROR: An error occurred while communicating with the API.", file=sys.stderr)
        print(f"Details: {e}", file=sys.stderr)
        if e.response is not None:
             print(f"Status Code: {e.response.status_code}", file=sys.stderr)
             print(f"Response Body: {e.response.text}", file=sys.stderr)

if __name__ == "__main__":
    args = parse_args()
    run_api_tests(
        base_url=args.host,
        vehicle_id=args.vehicle_id,
        api_key=args.api_key,
        page=args.page,
        limit=args.limit,
        start_date=args.start_date,
        end_date=args.end_date,
        daily_limit=args.daily_limit
    )