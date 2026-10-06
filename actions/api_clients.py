"""
api_clients.py - thin wrappers around the external APIs used by the bot.

Design rules applied to every client:
  * API keys are read from environment variables (never hard-coded).
  * Every HTTP call has a short timeout (default 2 s) so that the full bot
    response stays under the 3-second latency target.
  * One retry only, then the function returns None. Callers MUST handle None
    by falling back to the curated JSON dataset and telling the user that the
    figure is an estimate.

APIs:
  Climatiq            - carbon emission estimates per transport mode
  Amadeus (sandbox)   - hotel list / hotel offers / flight offers
  OpenRouteService    - road distance and duration
  OpenCage            - geocoding (place name <-> coordinates)
"""
import logging
import math
import os
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

TIMEOUT_S = float(os.getenv("API_TIMEOUT_SECONDS", "2"))


def _request(method: str, url: str, retries: int = 1, **kwargs) -> Optional[dict]:
    """Perform an HTTP request with timeout and a single retry.

    Returns parsed JSON on success, or None on any network/HTTP/JSON error.
    """
    for attempt in range(retries + 1):
        try:
            resp = requests.request(method, url, timeout=TIMEOUT_S, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as err:
            logger.warning("API call failed (%s %s, attempt %d): %s",
                           method, url, attempt + 1, err)
            if attempt < retries:
                time.sleep(0.2)          # brief back-off before the retry
    return None


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km. Used for flights and as routing fallback."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------- #
# Climatiq
# --------------------------------------------------------------------------- #
class ClimatiqClient:
    """Carbon estimates via POST /data/v1/estimate.

    Activity IDs below should be confirmed in the Climatiq Data Explorer for
    the data version you use; they can be overridden with env variables.
    """
    URL = "https://api.climatiq.io/data/v1/estimate"
    ACTIVITY_IDS = {
        "train": os.getenv("CLIMATIQ_ID_TRAIN",
                           "passenger_train-route_type_national_rail-fuel_source_na"),
        "coach": os.getenv("CLIMATIQ_ID_COACH",
                           "passenger_vehicle-vehicle_type_coach-fuel_source_na-distance_na-engine_size_na"),
        "car": os.getenv("CLIMATIQ_ID_CAR",
                         "passenger_vehicle-vehicle_type_car-fuel_source_na-engine_size_na-vehicle_age_na-vehicle_weight_na"),
        "plane": os.getenv("CLIMATIQ_ID_PLANE",
                           "passenger_flight-route_type_international-aircraft_type_na-distance_short_haul_lt_3700km-class_economy-rf_included-distance_uplift_included"),
    }

    def __init__(self) -> None:
        self.key = os.getenv("CLIMATIQ_API_KEY")

    def estimate_kg(self, mode: str, distance_km: float, passengers: int = 1) -> Optional[float]:
        """Return kg CO2e for the trip leg, or None if unavailable."""
        if not self.key or mode not in self.ACTIVITY_IDS:
            return None
        body = {
            "emission_factor": {"activity_id": self.ACTIVITY_IDS[mode], "data_version": "^21"},
            "parameters": {"passengers": passengers, "distance": round(distance_km, 1),
                           "distance_unit": "km"},
        }
        data = _request("POST", self.URL, json=body,
                        headers={"Authorization": f"Bearer {self.key}"})
        if data and isinstance(data.get("co2e"), (int, float)):
            return float(data["co2e"])
        return None


# --------------------------------------------------------------------------- #
# Amadeus for Developers (test / sandbox environment)
# --------------------------------------------------------------------------- #
class AmadeusClient:
    """OAuth2 client-credentials flow; the token is cached until expiry."""
    BASE = "https://test.api.amadeus.com"

    def __init__(self) -> None:
        self.key = os.getenv("AMADEUS_API_KEY")
        self.secret = os.getenv("AMADEUS_API_SECRET")
        self._token: Optional[str] = None
        self._expires = 0.0

    def _get_token(self) -> Optional[str]:
        if not (self.key and self.secret):
            return None
        if self._token and time.time() < self._expires - 30:
            return self._token
        data = _request("POST", f"{self.BASE}/v1/security/oauth2/token",
                        data={"grant_type": "client_credentials",
                              "client_id": self.key, "client_secret": self.secret})
        if not data or "access_token" not in data:
            return None
        self._token = data["access_token"]
        self._expires = time.time() + float(data.get("expires_in", 1799))
        return self._token

    def _get(self, path: str, params: Dict) -> Optional[dict]:
        token = self._get_token()
        if not token:
            return None
        return _request("GET", f"{self.BASE}{path}", params=params,
                        headers={"Authorization": f"Bearer {token}"})

    def hotels_by_city(self, city_code: str, limit: int = 10) -> Optional[List[Dict]]:
        """Hotel names/ids in a city (IATA city code, e.g. PAR)."""
        data = self._get("/v1/reference-data/locations/hotels/by-city",
                         {"cityCode": city_code, "radius": 5, "radiusUnit": "KM"})
        if not data or "data" not in data:
            return None
        return [{"hotel_id": h.get("hotelId"), "name": h.get("name", "").title()}
                for h in data["data"][:limit]]

    def cheapest_flight_eur(self, origin: str, dest: str, date: str) -> Optional[float]:
        """Cheapest one-way economy fare in EUR, or None."""
        data = self._get("/v2/shopping/flight-offers",
                         {"originLocationCode": origin, "destinationLocationCode": dest,
                          "departureDate": date, "adults": 1, "currencyCode": "EUR",
                          "max": 5})
        try:
            prices = [float(o["price"]["grandTotal"]) for o in data["data"]]
            return min(prices) if prices else None
        except (TypeError, KeyError, ValueError):
            return None


# --------------------------------------------------------------------------- #
# OpenRouteService
# --------------------------------------------------------------------------- #
class OpenRouteServiceClient:
    URL = "https://api.openrouteservice.org/v2/directions/driving-car"

    def __init__(self) -> None:
        self.key = os.getenv("ORS_API_KEY")

    def road_km(self, lat1, lng1, lat2, lng2) -> Optional[float]:
        """Road distance in km (coordinates are sent as [lng, lat])."""
        if not self.key:
            return None
        data = _request("POST", self.URL,
                        json={"coordinates": [[lng1, lat1], [lng2, lat2]]},
                        headers={"Authorization": self.key})
        try:
            return data["routes"][0]["summary"]["distance"] / 1000.0
        except (TypeError, KeyError, IndexError):
            return None


# --------------------------------------------------------------------------- #
# OpenCage geocoding
# --------------------------------------------------------------------------- #
class OpenCageClient:
    URL = "https://api.opencagedata.com/geocode/v1/json"

    def __init__(self) -> None:
        self.key = os.getenv("OPENCAGE_API_KEY")

    def geocode(self, query: str) -> Optional[Dict]:
        """Place name -> {name, lat, lng}. Coordinates rounded (privacy)."""
        if not self.key:
            return None
        data = _request("GET", self.URL, params={"q": query, "key": self.key,
                                                  "limit": 1, "no_annotations": 1})
        try:
            r = data["results"][0]
            return {"name": r["formatted"], "lat": round(r["geometry"]["lat"], 2),
                    "lng": round(r["geometry"]["lng"], 2)}
        except (TypeError, KeyError, IndexError):
            return None

    def reverse(self, lat: float, lng: float) -> Optional[str]:
        """Coordinates -> town name, used when the browser shares GPS."""
        res = self.geocode(f"{round(lat, 2)},{round(lng, 2)}")
        return res["name"] if res else None
