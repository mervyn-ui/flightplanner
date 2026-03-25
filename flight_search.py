import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import os as _os
API_KEY = _os.environ.get("SERPAPI_KEY", "4faef9dffd3c87ea6997ad5c8a16775e835982327e78f6f8175d5f5216d89716")


def search_flights(origin, destination, depart_date, return_date=None, adults=1, children=0):
    """Search one origin→destination combination via SerpAPI."""
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": depart_date,
        "currency": "EUR",
        "hl": "en",
        "stops": "1",
        "adults": max(1, int(adults)),
        "api_key": API_KEY,
    }
    if children and int(children) > 0:
        params["children_num"] = int(children)
    if return_date:
        params["return_date"] = return_date
        params["type"] = "1"   # round-trip
    else:
        params["type"] = "2"   # one-way

    try:
        response = requests.get("https://serpapi.com/search", params=params, timeout=20)
        data = response.json()

        if "error" in data:
            return origin, destination, None, data["error"]

        all_flights = data.get("best_flights", []) + data.get("other_flights", [])
        if not all_flights:
            return origin, destination, None, "No flights found"

        return origin, destination, all_flights, None

    except Exception as e:
        return origin, destination, None, str(e)


def format_duration(minutes):
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m"


def get_airports(label):
    while True:
        raw = input(f"{label} airport codes (e.g. JFK, EWR): ").strip().upper()
        airports = [a.strip() for a in raw.split(",") if a.strip()]
        if airports:
            return airports
        print("  Please enter at least one airport code.\n")


def get_date(label):
    while True:
        date = input(f"{label} date (YYYY-MM-DD): ").strip()
        if len(date) == 10 and date[4] == "-" and date[7] == "-":
            return date
        print("  Use format YYYY-MM-DD (e.g. 2026-04-15)\n")


def main():
    print()
    print("=" * 58)
    print("         Multi-Airport Flight Search Tool")
    print("=" * 58)
    print()

    if API_KEY == "YOUR_SERPAPI_KEY_HERE":
        print("ERROR: Please open flight_search.py and paste your SerpAPI key.")
        print("       Look for: API_KEY = \"YOUR_SERPAPI_KEY_HERE\"")
        return

    origins = get_airports("Origin")
    destinations = get_airports("Destination")
    print()

    trip_type = input("Trip type:\n  1 = One-way\n  2 = Round-trip\nChoice: ").strip()
    print()

    depart_date = get_date("Departure")
    return_date = get_date("Return") if trip_type == "2" else None
    print()

    # Build combinations (skip same origin/destination)
    combos = [(o, d) for o in origins for d in destinations if o != d]

    if not combos:
        print("No valid combinations (origin and destination can't be the same).")
        return

    print(f"Searching {len(combos)} combination(s) in parallel...\n")

    results = []

    # Search all combinations at the same time
    with ThreadPoolExecutor(max_workers=len(combos)) as executor:
        futures = {
            executor.submit(search_flights, o, d, depart_date, return_date): (o, d)
            for o, d in combos
        }
        for future in as_completed(futures):
            origin, dest, flight, error = future.result()
            if flight:
                results.append((origin, dest, flight))
            else:
                results.append((origin, dest, {"error": error}))

    # Sort by price (cheapest first)
    results.sort(key=lambda x: x[2].get("price", float("inf")))

    # Display results
    print("=" * 58)
    print(f"  RESULTS  (sorted cheapest → most expensive)")
    print("=" * 58)

    for i, (origin, dest, flight) in enumerate(results, 1):
        print(f"\n#{i}  {origin}  →  {dest}")
        print("-" * 40)

        if "error" in flight:
            print(f"  Could not fetch: {flight['error']}")
            continue

        price = flight.get("price", "N/A")
        print(f"  Price:    ${price}")

        legs = flight.get("flights", [])
        if legs:
            airline = legs[0].get("airline", "Unknown")
            flight_num = legs[0].get("flight_number", "")
            stops = len(legs) - 1
            total_duration = flight.get("total_duration", 0)

            print(f"  Airline:  {airline} {flight_num}")
            print(f"  Duration: {format_duration(total_duration)}")
            print(f"  Stops:    {'Nonstop' if stops == 0 else stops}")

            dep = legs[0].get("departure_airport", {})
            arr = legs[-1].get("arrival_airport", {})
            dep_time = dep.get("time", "")
            arr_time = arr.get("time", "")
            if dep_time and arr_time:
                print(f"  Times:    {dep_time}  →  {arr_time}")

    print()
    print("=" * 58)
    print("  Tip: Search kayak.com or google.com/flights to book.")
    print("=" * 58)
    print()


if __name__ == "__main__":
    main()
