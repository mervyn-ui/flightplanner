from flask import Flask, render_template, request, jsonify, redirect
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from flight_search import search_flights, format_duration, API_KEY
import uuid, base64, subprocess, tempfile, os

app = Flask(__name__)

# In-memory store for shared card sets (uuid → list of card dicts)
_shared_cards = {}


def parse_time(time_str):
    """Parse any time string into (hour, minute) integers. Returns (None, None) on failure."""
    import re
    if not time_str:
        return None, None
    m = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM)?', str(time_str), re.IGNORECASE)
    if not m:
        return None, None
    hour, minute = int(m.group(1)), int(m.group(2))
    period = m.group(3)
    if period:
        if period.upper() == "PM" and hour != 12:
            hour += 12
        elif period.upper() == "AM" and hour == 12:
            hour = 0
    return hour, minute


def add_minutes(hour, minute, delta):
    total = hour * 60 + minute + delta
    h, m = (total // 60) % 24, total % 60
    # Round to nearest 30-min interval (rentalcars.com requirement)
    m = 30 if m >= 15 else 0
    return h, m


def sub_minutes(hour, minute, delta):
    total = hour * 60 + minute - delta
    if total < 0:
        total += 24 * 60
    # Round UP to nearest 30-min (so dropoff is never more than 1h before departure)
    import math
    total = math.ceil(total / 30) * 30
    return (total // 60) % 24, total % 60




def adjacent_dates(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return [(d + timedelta(days=i)).strftime("%Y-%m-%d") for i in [-1, 0, 1]]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/search", methods=["POST"])
def search():
    data = request.json
    origins      = [o.strip().upper() for o in data.get("origins", "").split(",") if o.strip()]
    destinations = [d.strip().upper() for d in data.get("destinations", "").split(",") if d.strip()]
    depart_date  = data.get("depart_date", "")
    return_date  = data.get("return_date") or None
    adults       = max(1, int(data.get("adults", 1) or 1))
    children     = max(0, int(data.get("children", 0) or 0))

    if not origins or not destinations or not depart_date:
        return jsonify({"error": "Please fill in all required fields."}), 400

    combos = [(o, d) for o in origins for d in destinations if o != d]
    if not combos:
        return jsonify({"error": "No valid combinations found."}), 400

    # Build all tasks: main results + return legs + adjacent dates
    # Each task: (type, origin, dest, dep_date, ret_date, adults, children)
    tasks = []
    for o, d in combos:
        tasks.append(("main", o, d, depart_date, return_date, adults, children))
    if return_date:
        for o, d in combos:
            tasks.append(("ret_leg", d, o, return_date, None, adults, children))
    for adj in adjacent_dates(depart_date):
        if adj != depart_date:
            for o, d in combos:
                tasks.append(("adj_dep", o, d, adj, return_date, adults, children))
    if return_date:
        for adj in adjacent_dates(return_date):
            if adj != return_date:
                for o, d in combos:
                    tasks.append(("adj_ret", o, d, depart_date, adj, adults, children))

    main_results = []
    dep_prices   = {}
    ret_prices   = {}
    return_map   = {}  # (dest, origin) -> return leg data

    with ThreadPoolExecutor(max_workers=max(len(tasks), 1)) as executor:
        future_map = {
            executor.submit(search_flights, t[1], t[2], t[3], t[4], t[5], t[6]): t
            for t in tasks
        }
        for future in as_completed(future_map):
            task_type, origin, dest, dep, ret, _ad, _ch = future_map[future]
            _, _, flight, error = future.result()

            if not flight or error:
                if task_type == "main":
                    main_results.append({"origin": origin, "destination": dest, "error": error or "No flights found"})
                continue

            def fmt(h, m):
                return f"{h:02d}:{m:02d}" if h is not None else ""

            flight_list = flight if isinstance(flight, list) else [flight]

            if task_type == "main":
                for f in flight_list:
                    legs  = f.get("flights", [])
                    price = f.get("price")

                    dep_time_str = legs[0].get("departure_airport", {}).get("time", "") if legs else ""
                    arr_time_str = legs[-1].get("arrival_airport",  {}).get("time", "") if legs else ""
                    dep_h, dep_m = parse_time(dep_time_str)
                    arr_h, arr_m = parse_time(arr_time_str)

                    ret_legs    = f.get("return_flights", [])
                    ret_dep_str = ret_legs[0].get("departure_airport", {}).get("time", "") if ret_legs else ""
                    ret_arr_str = ret_legs[-1].get("arrival_airport",  {}).get("time", "") if ret_legs else ""
                    ret_dep_h, ret_dep_m = parse_time(ret_dep_str)
                    ret_arr_h, ret_arr_m = parse_time(ret_arr_str)

                    pu_h, pu_m = add_minutes(arr_h, arr_m, 20) if arr_h is not None else (10, 0)
                    if ret_dep_h is not None:
                        do_h, do_m = sub_minutes(ret_dep_h, ret_dep_m, 60)
                    elif dep_h is not None:
                        do_h, do_m = sub_minutes(dep_h, dep_m, 60)
                    else:
                        do_h, do_m = 9, 0

                    main_results.append({
                        "origin":        origin,
                        "destination":   dest,
                        "dep_airport":   legs[0].get("departure_airport", {}).get("id", origin) if legs else origin,
                        "arr_airport":   legs[-1].get("arrival_airport",  {}).get("id", dest)   if legs else dest,
                        "price":         price,
                        "airline":       legs[0].get("airline", "Unknown") if legs else "Unknown",
                        "flight_number": legs[0].get("flight_number", "") if legs else "",
                        "duration":      format_duration(f.get("total_duration", 0)),
                        "duration_mins": f.get("total_duration", 0),
                        "stops":         len(legs) - 1 if legs else 0,
                        "dep_time":      fmt(dep_h, dep_m),
                        "arr_time":      fmt(arr_h, arr_m),
                        "ret_dep_time":  fmt(ret_dep_h, ret_dep_m),
                        "ret_arr_time":  fmt(ret_arr_h, ret_arr_m),
                        "pu_hour":       pu_h,
                        "pu_minute":     pu_m,
                        "do_hour":       do_h,
                        "do_minute":     do_m,
                        "booking_token": f.get("booking_token", ""),
                    })
            elif task_type == "ret_leg":
                flight_list2 = flight if isinstance(flight, list) else [flight]
                best_ret = min(flight_list2, key=lambda f: f.get("price", float("inf")))
                ret_legs2   = best_ret.get("flights", [])
                ret_dep_str = ret_legs2[0].get("departure_airport", {}).get("time", "") if ret_legs2 else ""
                ret_arr_str = ret_legs2[-1].get("arrival_airport",  {}).get("time", "") if ret_legs2 else ""
                ret_dep_h, ret_dep_m = parse_time(ret_dep_str)
                ret_arr_h, ret_arr_m = parse_time(ret_arr_str)
                return_map[(origin, dest)] = {
                    "ret_dep_time": fmt(ret_dep_h, ret_dep_m),
                    "ret_arr_time": fmt(ret_arr_h, ret_arr_m),
                    "ret_dep_h":    ret_dep_h,
                    "ret_dep_m":    ret_dep_m,
                }
            elif task_type in ("adj_dep", "adj_ret"):
                flight_list3 = flight if isinstance(flight, list) else [flight]
                min_price = min((f.get("price", float("inf")) for f in flight_list3), default=float("inf"))
                if task_type == "adj_dep" and min_price < float("inf"):
                    dep_prices[dep] = min(dep_prices.get(dep, float("inf")), min_price)
                elif task_type == "adj_ret" and min_price < float("inf"):
                    ret_prices[ret] = min(ret_prices.get(ret, float("inf")), min_price)

    # Merge return leg data into main results and fix dropoff time
    for r in main_results:
        if "error" in r:
            continue
        ret = return_map.get((r["destination"], r["origin"]), {})
        r["ret_dep_time"] = ret.get("ret_dep_time", "")
        r["ret_arr_time"] = ret.get("ret_arr_time", "")
        if ret.get("ret_dep_h") is not None:
            r["do_hour"], r["do_minute"] = sub_minutes(ret["ret_dep_h"], ret["ret_dep_m"], 60)

    # Include the searched date in the departure price strip
    valid_main = [r for r in main_results if r.get("price")]
    if valid_main:
        main_min = min(r["price"] for r in valid_main)
        dep_prices[depart_date] = min(dep_prices.get(depart_date, float("inf")), main_min)

    # Sort by price (cheapest first)
    main_results.sort(key=lambda x: x.get("price") or float("inf"))

    # Build price strips sorted low → high
    dep_price_list = sorted(
        [{"date": d, "price": p, "selected": d == depart_date}
         for d, p in dep_prices.items() if p != float("inf")],
        key=lambda x: x["price"]
    )
    ret_price_list = sorted(
        [{"date": d, "price": p, "selected": d == return_date}
         for d, p in ret_prices.items() if p != float("inf")],
        key=lambda x: x["price"]
    )

    return jsonify({
        "results":     main_results,
        "dep_prices":  dep_price_list,
        "ret_prices":  ret_price_list,
        "depart_date": depart_date,
        "return_date": return_date,
    })


@app.route("/travel_guide")
def travel_guide():
    from zoneinfo import ZoneInfo
    import requests as req

    dep_airport  = request.args.get("dep_airport", "")
    dep_time     = request.args.get("dep_time", "")
    dep_date     = request.args.get("dep_date", "")
    arr_airport  = request.args.get("arr_airport", "")
    ret_dep_time = request.args.get("ret_dep_time", "")
    ret_dep_date = request.args.get("ret_dep_date", "")

    AIRPORT_COORDS = {
        "AMS": (52.308601,  4.763889),
        "BRU": (50.901402,  4.484440),
        "CRL": (50.459202,  4.453820),
        "EIN": (51.450101,  5.374520),
        "NRN": (51.602402,  6.142170),
        "OPO": (41.236698, -8.670960),
        "SCQ": (42.896388, -8.415139),
        "VGO": (42.232300, -8.626700),
        "LIS": (38.781311, -9.135921),
    }

    AIRPORT_NAMES = {
        "AMS": "Amsterdam Schiphol",
        "BRU": "Brussels Zaventem",
        "CRL": "Charleroi",
        "EIN": "Eindhoven",
        "NRN": "Weeze",
        "OPO": "Porto",
        "SCQ": "Santiago de Compostela",
        "VGO": "Vigo",
        "LIS": "Lisbon",
    }

    # Timezone per airport
    AIRPORT_TZ = {
        "AMS": "Europe/Amsterdam", "BRU": "Europe/Brussels",
        "CRL": "Europe/Brussels",  "EIN": "Europe/Amsterdam",
        "NRN": "Europe/Amsterdam", "OPO": "Europe/Lisbon",
        "SCQ": "Europe/Madrid",    "VGO": "Europe/Madrid",
        "LIS": "Europe/Lisbon",
    }

    def geocode_structured(pc, city):
        """Geocode using separate postcode and city values — avoids split issues with spaced postcodes (e.g. 'SW18 1AA')."""
        base = "https://nominatim.openstreetmap.org/search"
        hdrs = {"User-Agent": "FlightSearchApp/1.0"}
        # Try postcode + city first, then postcode alone, then free-text city
        candidates = []
        if pc and city:
            candidates.append({"postalcode": pc, "city": city})
        if pc:
            candidates.append({"postalcode": pc})
        if city:
            candidates.append({"q": city})
        for params in candidates:
            try:
                r = req.get(base, params={**params, "format": "json", "limit": 1},
                            headers=hdrs, timeout=6)
                d = r.json()
                if d:
                    return float(d[0]["lat"]), float(d[0]["lon"])
            except Exception:
                pass
        return None, None

    def geocode(q):
        """Free-text geocode fallback for legacy home_address param."""
        base = "https://nominatim.openstreetmap.org/search"
        hdrs = {"User-Agent": "FlightSearchApp/1.0"}
        try:
            r = req.get(base, params={"q": q, "format": "json", "limit": 1},
                        headers=hdrs, timeout=8)
            d = r.json()
            if d:
                return float(d[0]["lat"]), float(d[0]["lon"])
        except Exception:
            pass
        return None, None

    def get_drive_mins(from_lat, from_lon, to_lat, to_lon):
        try:
            r = req.get(
                f"http://router.project-osrm.org/route/v1/driving/{from_lon},{from_lat};{to_lon},{to_lat}",
                params={"overview": "false"}, timeout=10)
            return int(r.json()["routes"][0]["duration"] / 60)
        except Exception:
            return None

    def schedule(date_str, time_str, dep_airport, arr_airport, from_lat, from_lon, traveler_tz):
        """
        Returns drive time (always) and full schedule (when dep_time is known).
        """
        if dep_airport not in AIRPORT_COORDS:
            return None
        drive_mins = get_drive_mins(from_lat, from_lon, *AIRPORT_COORDS[dep_airport])
        if drive_mins is None:
            return None

        base = {
            "date":             date_str,
            "drive_mins":       drive_mins,
            "dep_airport_name": AIRPORT_NAMES.get(dep_airport, dep_airport),
            "arr_airport_name": AIRPORT_NAMES.get(arr_airport, arr_airport),
            "departure":        time_str,
        }

        if not date_str or not time_str:
            return base  # drive info only — no full schedule without departure time

        # Parse departure in airport's local timezone
        airport_tz          = ZoneInfo(AIRPORT_TZ.get(dep_airport, "UTC"))
        dep_naive           = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        dep_aware           = dep_naive.replace(tzinfo=airport_tz)

        # Subtract: 1.5h buffer + drive + 15min parking → leave time in airport tz
        leave_airport_tz    = dep_aware - timedelta(minutes=90 + drive_mins + 15)
        arrive_airport_tz   = dep_aware - timedelta(minutes=90)

        # Convert to traveler's local timezone
        traveler_zone       = ZoneInfo(traveler_tz)
        leave_local         = leave_airport_tz.astimezone(traveler_zone)
        arrive_local        = arrive_airport_tz.astimezone(traveler_zone)

        return {**base,
            "leave":          leave_local.strftime("%H:%M"),
            "arrive_airport": arrive_local.strftime("%H:%M"),
        }

    # Outbound: full schedule when home address provided, basic flight info otherwise
    home_pc      = request.args.get("home_pc",      "").strip()
    home_city    = request.args.get("home_city",    "").strip()
    home_address = request.args.get("home_address", "").strip()  # legacy fallback
    if home_pc or home_city:
        home_lat, home_lon = geocode_structured(home_pc, home_city)
        if home_lat is None:
            home_lat, home_lon = 52.0705, 4.3007   # fallback: Den Haag centre
        out = schedule(dep_date, dep_time, dep_airport, arr_airport, home_lat, home_lon, "Europe/Amsterdam")
    elif home_address:
        home_lat, home_lon = geocode(home_address)
        if home_lat is None:
            home_lat, home_lon = 52.0705, 4.3007
        out = schedule(dep_date, dep_time, dep_airport, arr_airport, home_lat, home_lon, "Europe/Amsterdam")
    elif dep_date or dep_time:
        out = {
            "date":             dep_date,
            "dep_airport_name": AIRPORT_NAMES.get(dep_airport, dep_airport),
            "arr_airport_name": AIRPORT_NAMES.get(arr_airport, arr_airport),
            "departure":        dep_time,
        }
    else:
        out = None

    # Return: only compute when both date and time are known
    # Use the destination airport's own coordinates as starting point
    # (traveller is near the airport — gives a sensible ~15 min buffer)
    if ret_dep_date and ret_dep_time and arr_airport in AIRPORT_COORDS:
        ret_lat, ret_lon = AIRPORT_COORDS[arr_airport]
        ret_tz = AIRPORT_TZ.get(arr_airport, "Europe/Madrid")
        ret = schedule(ret_dep_date, ret_dep_time, arr_airport, dep_airport, ret_lat, ret_lon, ret_tz)
    else:
        ret = None

    return jsonify({"outbound": out, "return": ret})


@app.route("/scan_best_deals")
def scan_best_deals():
    orig_param   = request.args.get("origins",      "BRU,EIN,AMS,NRN")
    dest_param   = request.args.get("destinations", "OPO,SCQ")
    origins      = [x.strip().upper() for x in orig_param.split(",") if x.strip()]
    destinations = [x.strip().upper() for x in dest_param.split(",") if x.strip()]
    combos       = [(o, d) for o in origins for d in destinations]

    # One Friday per month for the next 12 months
    today = datetime.today()
    scan_dates = []
    for m in range(1, 4):
        d = (today.replace(day=1) + timedelta(days=32 * m)).replace(day=1)
        while d.weekday() != 4:   # find first Friday
            d += timedelta(days=1)
        scan_dates.append(d)

    # Build tasks: each combo × each month, 5-day stay
    tasks = []
    for dep_dt in scan_dates:
        ret_dt = dep_dt + timedelta(days=5)
        dep_str = dep_dt.strftime("%Y-%m-%d")
        ret_str = ret_dt.strftime("%Y-%m-%d")
        for o, d in combos:
            tasks.append((o, d, dep_str, ret_str))

    deals = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        future_map = {
            executor.submit(search_flights, t[0], t[1], t[2], t[3]): t
            for t in tasks
        }
        for future in as_completed(future_map):
            o, d, dep, ret = future_map[future]
            _, _, flight, error = future.result()
            if not flight or error:
                continue
            flight_list = flight if isinstance(flight, list) else [flight]
            for f in flight_list:
                price = f.get("price")
                if not price:
                    continue
                legs = f.get("flights", [])
                dep_time_str = legs[0].get("departure_airport", {}).get("time", "") if legs else ""
                dep_h, dep_m = parse_time(dep_time_str)
                def fmt(h, m): return f"{h:02d}:{m:02d}" if h is not None else ""
                deals.append({
                    "origin":        o,
                    "destination":   d,
                    "depart_date":   dep,
                    "return_date":   ret,
                    "price":         price,
                    "airline":       legs[0].get("airline", "Unknown") if legs else "Unknown",
                    "dep_time":      fmt(dep_h, dep_m),
                    "duration":      format_duration(f.get("total_duration", 0)),
                    "booking_token": f.get("booking_token", ""),
                })

    deals.sort(key=lambda x: x["price"])
    return jsonify({"deals": deals[:3]})


@app.route("/share_email", methods=["POST"])
def share_email():
    """Copy HTML to clipboard, open Gmail compose, then auto-paste into the body."""
    html = (request.json or {}).get("html", "")
    if not html:
        return jsonify({"ok": False, "error": "No HTML"})
    try:
        tmp_path = os.path.join(tempfile.gettempdir(), "flightcard_email.html")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(html)
        gmail_url = "https://mail.google.com/mail/?view=cm&fs=1"
        script = f'''
set the clipboard to (read (POSIX file "{tmp_path}") as «class HTML»)
delay 0.3
open location "{gmail_url}"
delay 5
tell application "System Events"
    key code 48
    key code 48
    keystroke "v" using command down
end tell
'''
        subprocess.Popen(["osascript", "-e", script])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/open_url", methods=["POST"])
def open_url():
    """Open a URL in the system's default browser (bypasses PyWebView)."""
    import webbrowser
    url = (request.json or {}).get("url", "")
    if not url or not url.startswith("http"):
        return jsonify({"ok": False, "error": "Invalid URL"})
    webbrowser.open(url)
    return jsonify({"ok": True})


@app.route("/copy_html", methods=["POST"])
def copy_html():
    """Copy HTML content to macOS clipboard so it can be pasted into Gmail as rich text."""
    html = (request.json or {}).get("html", "")
    if not html:
        return jsonify({"ok": False, "error": "No HTML"})
    try:
        tmp_path = os.path.join(tempfile.gettempdir(), "flightsearch_email.html")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(html)
        result = subprocess.run(
            ["osascript", "-e",
             f'set the clipboard to (read (POSIX file "{tmp_path}") as «class HTML»)'],
            capture_output=True
        )
        if result.returncode == 0:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": result.stderr.decode()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/copy_image", methods=["POST"])
def copy_image():
    """Receive a base64 PNG data URL and copy it to the macOS clipboard via osascript."""
    data = (request.json or {}).get("image", "")
    if not data:
        return jsonify({"ok": False, "error": "No image data"})
    # Strip data URL prefix if present
    if "," in data:
        data = data.split(",", 1)[1]
    try:
        img_bytes = base64.b64decode(data)
        tmp_path = os.path.join(tempfile.gettempdir(), "flightsearch_copy.png")
        with open(tmp_path, "wb") as f:
            f.write(img_bytes)
        result = subprocess.run(
            ["osascript", "-e",
             f'set the clipboard to (read (POSIX file "{tmp_path}") as «class PNGf»)'],
            capture_output=True
        )
        if result.returncode == 0:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": result.stderr.decode()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/share", methods=["POST"])
def share():
    cards = request.json
    if not cards or not isinstance(cards, list):
        return jsonify({"error": "No card data"}), 400
    share_id = str(uuid.uuid4())
    _shared_cards[share_id] = cards
    return jsonify({"url": f"/view/{share_id}"})


@app.route("/view/<share_id>")
def view_share(share_id):
    cards = _shared_cards.get(share_id)
    if not cards:
        return "Share link not found or expired.", 404
    return render_template("share.html", cards=cards)


@app.route("/booking_link")
def booking_link():
    import requests as req
    token = request.args.get("token", "")
    fallback = request.args.get("fallback", "/")
    if not token:
        return redirect(fallback)
    try:
        resp = req.get("https://serpapi.com/search", params={
            "engine": "google_flights_booking_links",
            "booking_token": token,
            "api_key": API_KEY,
        }, timeout=10)
        data = resp.json()
        options = data.get("booking_options", [])
        for opt in options:
            for detail in opt.get("details", []):
                link = detail.get("link", "")
                if link:
                    return redirect(link)
    except Exception:
        pass
    return redirect(fallback or "https://www.booking.com/flights/")


if __name__ == "__main__":
    app.run(debug=True, port=5050)
