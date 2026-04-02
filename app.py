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
        # Netherlands
        "AMS": (52.308601,  4.763889), "EIN": (51.450101,  5.374520),
        "RTM": (51.956900,  4.437200), "NRN": (51.602402,  6.142170),
        "MST": (50.911700,  5.770100), "GRQ": (53.119700,  6.579400),
        # Belgium / Luxembourg
        "BRU": (50.901402,  4.484440), "CRL": (50.459202,  4.453820),
        "LGG": (50.637400,  5.443200), "ANR": (51.189400,  4.460300),
        "LUX": (49.623400,  6.204400),
        # Germany
        "FRA": (50.037900,  8.562200), "MUC": (48.353800, 11.786100),
        "BER": (52.366700, 13.503300), "DUS": (51.289500,  6.766800),
        "CGN": (50.865900,  7.142700), "HAM": (53.630400,  9.988200),
        "HAJ": (52.461100,  9.685000), "STR": (48.689900,  9.222000),
        "NUE": (49.498700, 11.066900), "HHN": (49.948700,  7.263900),
        "FKB": (48.779300,  8.080500), "LEJ": (51.432400, 12.241600),
        "DTM": (51.518300,  7.612400), "SCN": (49.214600,  7.109600),
        # France
        "CDG": (49.009700,  2.547900), "ORY": (48.723300,  2.379400),
        "NCE": (43.658400,  7.215900), "MRS": (43.439300,  5.221400),
        "LYS": (45.725600,  5.081100), "BOD": (44.828300, -0.715500),
        "TLS": (43.629300,  1.367800), "NTE": (47.153300, -1.611100),
        "SXB": (48.538200,  7.628300), "LIL": (50.563000,  3.089400),
        "BES": (48.447800, -4.418600),
        # UK
        "LHR": (51.477500, -0.461400), "LGW": (51.148100, -0.190300),
        "STN": (51.885000,  0.235000), "MAN": (53.353700, -2.275000),
        "LTN": (51.874700, -0.368300), "BHX": (52.453900, -1.748000),
        "EDI": (55.950000, -3.372500), "GLA": (55.871900, -4.433100),
        "BRS": (51.382700, -2.719100), "NCL": (54.965300, -1.691700),
        "LPL": (53.333600, -2.849700), "LBA": (53.865900, -1.660600),
        "ABZ": (57.201900, -2.197800), "BFS": (54.657500, -6.215800),
        "BHD": (54.618100, -5.872500), "SOU": (50.950300, -1.356800),
        "EXT": (50.734400, -3.413900),
        # Ireland
        "DUB": (53.421300, -6.270000), "ORK": (51.841300, -8.491200),
        "SNN": (52.702000, -8.924800),
        # Portugal
        "OPO": (41.236698, -8.670960), "LIS": (38.781311, -9.135921),
        "FAO": (37.014500, -7.965900), "PDL": (37.741200,-25.697900),
        "FNC": (32.697900,-16.778100),
        # Spain mainland
        "MAD": (40.471926, -3.562640), "BCN": (41.297078,  2.078464),
        "AGP": (36.674900, -4.499100), "VLC": (39.489300, -0.481600),
        "SVQ": (37.418000, -5.893100), "BIO": (43.301100, -2.910600),
        "SDR": (43.427100, -3.820000), "OVD": (43.563600, -6.034600),
        "SCQ": (42.896388, -8.415139), "VGO": (42.232300, -8.626700),
        "PMI": (39.551700,  2.738800), "IBZ": (38.872900,  1.373100),
        "ALC": (38.282200, -0.558200), "GRO": (41.901100,  2.760300),
        "ZAZ": (41.666300, -1.041500), "XRY": (36.744600, -6.060100),
        "GRX": (37.188700, -3.777400), "MAH": (39.862600,  4.218600),
        # Canary Islands (Atlantic/Canary tz)
        "LPA": (27.931900,-15.386600), "TFS": (28.044500,-16.572500),
        "TFN": (28.482700,-16.341400), "FUE": (28.452700,-13.863800),
        "ACE": (28.945500,-13.605200),
        # Italy
        "FCO": (41.800300, 12.238900), "CIA": (41.799400, 12.594900),
        "MXP": (45.630600,  8.728100), "LIN": (45.445300,  9.276800),
        "BGY": (45.673900,  9.704200), "VCE": (45.505300, 12.351900),
        "TSF": (45.648400, 12.194200), "NAP": (40.886000, 14.290800),
        "CTA": (37.466800, 15.066400), "PMO": (38.179600, 13.091000),
        "BRI": (41.138900, 16.760800), "VRN": (45.395700, 10.888500),
        "BLQ": (44.535400, 11.288700), "PSA": (43.683900, 10.392700),
        # Scandinavia
        "OSL": (60.193900, 11.100400), "BGO": (60.293400,  5.218100),
        "SVG": (58.876800,  5.637800), "TRD": (63.457800, 10.923900),
        "ARN": (59.651900, 17.918600), "GOT": (57.662800, 12.279800),
        "MMX": (55.536300, 13.376200), "CPH": (55.618000, 12.656100),
        "BLL": (55.740300,  9.151900), "AAL": (57.092600,  9.849200),
        # Finland / Baltics
        "HEL": (60.317200, 24.963300), "TMP": (61.414100, 23.604400),
        "OUL": (64.930100, 25.354600), "RIX": (56.923600, 23.971100),
        "VNO": (54.634100, 25.285800), "TLL": (59.413300, 24.832800),
        # Switzerland / Austria
        "ZRH": (47.464700,  8.549200), "GVA": (46.238100,  6.108900),
        "BSL": (47.589600,  7.529900), "VIE": (48.110300, 16.569700),
        "SZG": (47.793300, 13.004300), "INN": (47.260200, 11.344000),
        "GRZ": (46.991100, 15.439600), "LNZ": (48.233200, 14.187500),
        # Central / Eastern Europe
        "PRG": (50.100800, 14.260000), "BTS": (48.170200, 17.212700),
        "BRQ": (49.151300, 16.694400), "WAW": (52.165700, 20.967100),
        "KRK": (50.077800, 19.784800), "GDN": (54.377600, 18.466200),
        "WRO": (51.102700, 16.885800), "POZ": (52.421000, 16.826300),
        "KTW": (50.474300, 19.080000), "BUD": (47.429800, 19.261100),
        "OTP": (44.571100, 26.085000), "CLJ": (46.785200, 23.686200),
        "SOF": (42.696700, 23.411400),
        # Balkans
        "BEG": (44.818400, 20.309100), "LJU": (46.223700, 14.457600),
        "ZAG": (45.742900, 16.068800), "SPU": (43.538900, 16.298000),
        "DBV": (42.561400, 18.268200), "TGD": (42.359400, 19.251900),
        "TIA": (41.414700, 19.720600), "SKP": (41.961600, 21.621400),
        "SJJ": (43.824600, 18.331500),
        # Greece / Cyprus
        "ATH": (37.936400, 23.944500), "SKG": (40.519700, 22.970900),
        "HER": (35.339700, 25.180300), "RHO": (36.405400, 28.086200),
        "CFU": (39.601900, 19.911700), "KGS": (36.793300, 26.717300),
        "JMK": (37.435100, 25.348100), "JTR": (36.399200, 25.479300),
        "CHQ": (35.531700, 24.149700), "LCA": (34.875100, 33.624900),
        "PFO": (34.717900, 32.485700),
        # Turkey / Iceland / Malta / N.Africa
        "IST": (41.275300, 28.751900), "SAW": (40.898600, 29.309200),
        "ADB": (38.292400, 27.157000), "AYT": (36.898700, 30.799200),
        "KEF": (63.985000,-22.605600), "MLA": (35.857400, 14.477500),
        "CMN": (33.367500, -7.589700), "RAK": (31.606900, -8.036300),
        "AGA": (30.325000, -9.413000), "TUN": (36.851000, 10.227200),
    }

    AIRPORT_NAMES = {
        "AMS": "Amsterdam Schiphol",   "EIN": "Eindhoven",
        "RTM": "Rotterdam",            "NRN": "Weeze",
        "MST": "Maastricht",           "GRQ": "Groningen",
        "BRU": "Brussels Zaventem",    "CRL": "Charleroi",
        "LGG": "Liège",                "ANR": "Antwerp",
        "LUX": "Luxembourg",
        "FRA": "Frankfurt",            "MUC": "Munich",
        "BER": "Berlin Brandenburg",   "DUS": "Düsseldorf",
        "CGN": "Cologne Bonn",         "HAM": "Hamburg",
        "HAJ": "Hannover",             "STR": "Stuttgart",
        "NUE": "Nuremberg",            "HHN": "Frankfurt Hahn",
        "FKB": "Baden-Baden",          "LEJ": "Leipzig Halle",
        "DTM": "Dortmund",             "SCN": "Saarbrücken",
        "CDG": "Paris CDG",            "ORY": "Paris Orly",
        "NCE": "Nice",                 "MRS": "Marseille",
        "LYS": "Lyon",                 "BOD": "Bordeaux",
        "TLS": "Toulouse",             "NTE": "Nantes",
        "SXB": "Strasbourg",           "LIL": "Lille",
        "BES": "Brest",
        "LHR": "London Heathrow",      "LGW": "London Gatwick",
        "STN": "London Stansted",      "MAN": "Manchester",
        "LTN": "London Luton",         "BHX": "Birmingham",
        "EDI": "Edinburgh",            "GLA": "Glasgow",
        "BRS": "Bristol",              "NCL": "Newcastle",
        "LPL": "Liverpool",            "LBA": "Leeds Bradford",
        "ABZ": "Aberdeen",             "BFS": "Belfast Intl",
        "BHD": "Belfast City",         "SOU": "Southampton",
        "EXT": "Exeter",
        "DUB": "Dublin",               "ORK": "Cork",
        "SNN": "Shannon",
        "OPO": "Porto",                "LIS": "Lisbon",
        "FAO": "Faro",                 "PDL": "Azores (Ponta Delgada)",
        "FNC": "Madeira (Funchal)",
        "MAD": "Madrid Barajas",       "BCN": "Barcelona El Prat",
        "AGP": "Málaga",               "VLC": "Valencia",
        "SVQ": "Seville",              "BIO": "Bilbao",
        "SDR": "Santander",            "OVD": "Asturias",
        "SCQ": "Santiago de Compostela","VGO": "Vigo",
        "PMI": "Palma de Mallorca",    "IBZ": "Ibiza",
        "ALC": "Alicante",             "GRO": "Girona",
        "ZAZ": "Zaragoza",             "XRY": "Jerez",
        "GRX": "Granada",              "MAH": "Menorca",
        "LPA": "Gran Canaria",         "TFS": "Tenerife South",
        "TFN": "Tenerife North",       "FUE": "Fuerteventura",
        "ACE": "Lanzarote",
        "FCO": "Rome Fiumicino",       "CIA": "Rome Ciampino",
        "MXP": "Milan Malpensa",       "LIN": "Milan Linate",
        "BGY": "Milan Bergamo",        "VCE": "Venice",
        "TSF": "Venice Treviso",       "NAP": "Naples",
        "CTA": "Catania",              "PMO": "Palermo",
        "BRI": "Bari",                 "VRN": "Verona",
        "BLQ": "Bologna",              "PSA": "Pisa",
        "OSL": "Oslo Gardermoen",      "BGO": "Bergen",
        "SVG": "Stavanger",            "TRD": "Trondheim",
        "ARN": "Stockholm Arlanda",    "GOT": "Gothenburg",
        "MMX": "Malmö",                "CPH": "Copenhagen",
        "BLL": "Billund",              "AAL": "Aalborg",
        "HEL": "Helsinki",             "TMP": "Tampere",
        "OUL": "Oulu",
        "RIX": "Riga",                 "VNO": "Vilnius",
        "TLL": "Tallinn",
        "ZRH": "Zurich",               "GVA": "Geneva",
        "BSL": "Basel",                "VIE": "Vienna",
        "SZG": "Salzburg",             "INN": "Innsbruck",
        "GRZ": "Graz",                 "LNZ": "Linz",
        "PRG": "Prague",               "BTS": "Bratislava",
        "BRQ": "Brno",                 "WAW": "Warsaw",
        "KRK": "Krakow",               "GDN": "Gdansk",
        "WRO": "Wroclaw",              "POZ": "Poznan",
        "KTW": "Katowice",             "BUD": "Budapest",
        "OTP": "Bucharest",            "CLJ": "Cluj-Napoca",
        "SOF": "Sofia",                "BEG": "Belgrade",
        "LJU": "Ljubljana",            "ZAG": "Zagreb",
        "SPU": "Split",                "DBV": "Dubrovnik",
        "TGD": "Podgorica",            "TIA": "Tirana",
        "SKP": "Skopje",               "SJJ": "Sarajevo",
        "ATH": "Athens",               "SKG": "Thessaloniki",
        "HER": "Heraklion",            "RHO": "Rhodes",
        "CFU": "Corfu",                "KGS": "Kos",
        "JMK": "Mykonos",              "JTR": "Santorini",
        "CHQ": "Chania",               "LCA": "Larnaca",
        "PFO": "Paphos",               "IST": "Istanbul",
        "SAW": "Istanbul Sabiha",      "ADB": "Izmir",
        "AYT": "Antalya",              "KEF": "Reykjavik",
        "MLA": "Malta",
        "CMN": "Casablanca",           "RAK": "Marrakech",
        "AGA": "Agadir",               "TUN": "Tunis",
    }

    # Timezone per airport
    AIRPORT_TZ = {
        **{k: "Europe/Amsterdam" for k in ["AMS","EIN","RTM","NRN","MST","GRQ"]},
        **{k: "Europe/Brussels"  for k in ["BRU","CRL","LGG","ANR","LUX"]},
        **{k: "Europe/Berlin"    for k in ["FRA","MUC","BER","DUS","CGN","HAM","HAJ","STR","NUE","HHN","FKB","LEJ","DTM","SCN"]},
        **{k: "Europe/Paris"     for k in ["CDG","ORY","NCE","MRS","LYS","BOD","TLS","NTE","SXB","LIL","BES"]},
        **{k: "Europe/London"    for k in ["LHR","LGW","STN","MAN","LTN","BHX","EDI","GLA","BRS","NCL","LPL","LBA","ABZ","BFS","BHD","SOU","EXT"]},
        **{k: "Europe/Dublin"    for k in ["DUB","ORK","SNN"]},
        **{k: "Europe/Lisbon"    for k in ["OPO","LIS","FAO","PDL","FNC"]},
        **{k: "Atlantic/Canary"  for k in ["LPA","TFS","TFN","FUE","ACE"]},
        **{k: "Europe/Madrid"    for k in ["MAD","BCN","AGP","VLC","SVQ","BIO","SDR","OVD","SCQ","VGO","PMI","IBZ","ALC","GRO","ZAZ","XRY","GRX","MAH"]},
        **{k: "Europe/Rome"      for k in ["FCO","CIA","MXP","LIN","BGY","VCE","TSF","NAP","CTA","PMO","BRI","VRN","BLQ","PSA"]},
        **{k: "Europe/Oslo"      for k in ["OSL","BGO","SVG","TRD"]},
        **{k: "Europe/Stockholm" for k in ["ARN","GOT","MMX"]},
        **{k: "Europe/Copenhagen" for k in ["CPH","BLL","AAL"]},
        **{k: "Europe/Helsinki"  for k in ["HEL","TMP","OUL"]},
        **{k: "Europe/Riga"      for k in ["RIX"]},
        **{k: "Europe/Vilnius"   for k in ["VNO"]},
        **{k: "Europe/Tallinn"   for k in ["TLL"]},
        **{k: "Europe/Zurich"    for k in ["ZRH","GVA","BSL"]},
        **{k: "Europe/Vienna"    for k in ["VIE","SZG","INN","GRZ","LNZ"]},
        **{k: "Europe/Prague"    for k in ["PRG","BRQ"]},
        **{k: "Europe/Bratislava" for k in ["BTS"]},
        **{k: "Europe/Warsaw"    for k in ["WAW","KRK","GDN","WRO","POZ","KTW"]},
        **{k: "Europe/Budapest"  for k in ["BUD"]},
        **{k: "Europe/Bucharest" for k in ["OTP","CLJ"]},
        **{k: "Europe/Sofia"     for k in ["SOF"]},
        **{k: "Europe/Belgrade"  for k in ["BEG","SJJ"]},
        **{k: "Europe/Ljubljana" for k in ["LJU"]},
        **{k: "Europe/Zagreb"    for k in ["ZAG","SPU","DBV"]},
        **{k: "Europe/Podgorica" for k in ["TGD"]},
        **{k: "Europe/Tirane"    for k in ["TIA"]},
        **{k: "Europe/Skopje"    for k in ["SKP"]},
        **{k: "Europe/Athens"    for k in ["ATH","SKG","HER","RHO","CFU","KGS","JMK","JTR","CHQ"]},
        **{k: "Asia/Nicosia"     for k in ["LCA","PFO"]},
        **{k: "Europe/Istanbul"  for k in ["IST","SAW","ADB","AYT"]},
        **{k: "Atlantic/Reykjavik" for k in ["KEF"]},
        **{k: "Europe/Malta"     for k in ["MLA"]},
        **{k: "Africa/Casablanca" for k in ["CMN","RAK","AGA"]},
        **{k: "Africa/Tunis"     for k in ["TUN"]},
    }

    def geocode_structured(pc, city):
        """Geocode using separate postcode and city values — avoids split issues with spaced postcodes (e.g. 'SW18 1AA')."""
        base = "https://nominatim.openstreetmap.org/search"
        hdrs = {"User-Agent": "FlightSearchApp/1.0"}
        candidates = []
        # Structured queries first (most precise)
        if pc and city:
            candidates.append({"postalcode": pc, "city": city})
        if pc:
            candidates.append({"postalcode": pc})
        # Free-text fallbacks — comma-separated works well for Nominatim
        if pc and city:
            candidates.append({"q": f"{pc}, {city}"})
            candidates.append({"q": f"{pc} {city}"})
        if city:
            candidates.append({"q": city})
        for params in candidates:
            try:
                r = req.get(base, params={**params, "format": "json", "limit": 1},
                            headers=hdrs, timeout=8)
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
        Falls back to basic flight info if drive time can't be calculated.
        """
        base_only = {
            "date":             date_str,
            "dep_airport_name": AIRPORT_NAMES.get(dep_airport, dep_airport),
            "arr_airport_name": AIRPORT_NAMES.get(arr_airport, arr_airport),
            "departure":        time_str,
        }
        if dep_airport not in AIRPORT_COORDS:
            return base_only
        drive_mins = get_drive_mins(from_lat, from_lon, *AIRPORT_COORDS[dep_airport])
        if drive_mins is None:
            return base_only

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
            out = {
                "date":             dep_date,
                "dep_airport_name": AIRPORT_NAMES.get(dep_airport, dep_airport),
                "arr_airport_name": AIRPORT_NAMES.get(arr_airport, arr_airport),
                "departure":        dep_time,
                "geocode_error":    True,
            }
        else:
            out = schedule(dep_date, dep_time, dep_airport, arr_airport, home_lat, home_lon, "Europe/Amsterdam")
            if out:
                out["home_label"] = f"{home_pc} {home_city}".strip()
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

    # Return: drive from Spain home (Bueu, Pontevedra) to departure airport.
    SPAIN_HOME_LAT, SPAIN_HOME_LON = 42.3272768, -8.7863966
    if ret_dep_date and ret_dep_time:
        ret_tz_str = AIRPORT_TZ.get(arr_airport, "Europe/Madrid")
        ret = schedule(ret_dep_date, ret_dep_time, arr_airport, dep_airport,
                       SPAIN_HOME_LAT, SPAIN_HOME_LON, ret_tz_str)
        if ret:
            try:
                ret_aware = datetime.strptime(
                    f"{ret_dep_date} {ret_dep_time}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=ZoneInfo(ret_tz_str))
                ret["tz"] = ret_aware.strftime("%Z")
            except Exception:
                ret["tz"] = ""
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


@app.route("/scan_timeline")
def scan_timeline():
    orig_param = request.args.get("origins", "BRU,EIN,AMS,NRN")
    dest_param = request.args.get("destinations", "OPO,SCQ,VGO")
    origins      = [x.strip().upper() for x in orig_param.split(",") if x.strip()]
    destinations = [x.strip().upper() for x in dest_param.split(",") if x.strip()]
    combos = [(o, d) for o in origins for d in destinations if o != d]

    today = datetime.today()
    # One departure date every 7 days, starting 5 days from now, for 90 days
    dep_dates = []
    d = today + timedelta(days=5)
    while (d - today).days <= 93:
        dep_dates.append(d)
        d += timedelta(days=3)

    durations = [3, 5, 7]   # 3 durations keeps API calls reasonable

    tasks = []
    for dep_dt in dep_dates:
        dep_str = dep_dt.strftime("%Y-%m-%d")
        for dur in durations:
            ret_str = (dep_dt + timedelta(days=dur)).strftime("%Y-%m-%d")
            for (o, dst) in combos:
                tasks.append((o, dst, dep_str, ret_str, dur))

    trips = []
    with ThreadPoolExecutor(max_workers=min(len(tasks), 40)) as executor:
        future_map = {
            executor.submit(search_flights, t[0], t[1], t[2], t[3]): t
            for t in tasks
        }
        for future in as_completed(future_map):
            o, dst, dep, ret, dur = future_map[future]
            _, _, flight, error = future.result()
            if not flight or error:
                continue
            flight_list = flight if isinstance(flight, list) else [flight]
            best = min(flight_list, key=lambda f: f.get("price") or float("inf"))
            price = best.get("price")
            if not price:
                continue
            legs = best.get("flights", [])
            dep_time_str = legs[0].get("departure_airport", {}).get("time", "") if legs else ""
            dep_h, dep_m = parse_time(dep_time_str)
            trips.append({
                "origin":        o,
                "destination":   dst,
                "dep_date":      dep,
                "ret_date":      ret,
                "duration":      dur,
                "price":         price,
                "airline":       legs[0].get("airline", "") if legs else "",
                "dep_time":      f"{dep_h:02d}:{dep_m:02d}" if dep_h is not None else "",
                "booking_token": best.get("booking_token", ""),
            })

    start_str = (today + timedelta(days=5)).strftime("%Y-%m-%d")
    end_str   = (today + timedelta(days=95)).strftime("%Y-%m-%d")
    return jsonify({"trips": trips, "start_date": start_str, "end_date": end_str})


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
