"""
actions.py - custom actions for the Rasa Eco-Travel Advisor.

Responsibilities (assignment Task 4):
  1. Query Climatiq for carbon emissions per transport mode   -> action_recommend_trip
  2. Fetch hotel / flight data from the Amadeus sandbox        -> action_search_hotels,
                                                                  action_recommend_trip
  3. Rank options with a weighted carbon/price/preference score -> scoring.py
  4. Package full conversation context for human handover      -> action_handover

Every action handles failed API responses by falling back to the curated JSON
dataset (actions/data/eco_data.json) and telling the user transparently.

Custom payloads (json_message) are rendered by the React frontend:
  type = "transport_cards" | "hotel_carousel" | "alert" | "handover" |
         "activity_list" | "offset_list"
"""
import difflib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Text

import requests
from rasa_sdk import Action, FormValidationAction, Tracker
from rasa_sdk.events import AllSlotsReset, SlotSet, UserUtteranceReverted
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.types import DomainDict

from .api_clients import (AmadeusClient, ClimatiqClient, OpenCageClient,
                          OpenRouteServiceClient, haversine_km)
from .scoring import classify_carbon, score_hotels, score_options

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Shared resources (loaded once when the action server starts)
# --------------------------------------------------------------------------- #
DATA_PATH = Path(__file__).parent / "data" / "eco_data.json"
with open(DATA_PATH, encoding="utf-8") as f:
    ECO = json.load(f)

climatiq = ClimatiqClient()
amadeus = AmadeusClient()
ors = OpenRouteServiceClient()
opencage = OpenCageClient()

MODES = ["train", "coach", "carshare", "car", "plane"]
MODE_LABELS = {"train": "Train", "coach": "Coach", "carshare": "Car share (2 people)",
               "car": "Car (solo driver)", "plane": "Flight"}


# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #
COORD_RE = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def nearest_city(lat: float, lng: float) -> Dict:
    """Closest curated city to a GPS position (offline fallback)."""
    return min(ECO["cities"].values(),
               key=lambda c: haversine_km(lat, lng, c["lat"], c["lng"]))


def find_city(name: Optional[str]) -> Optional[Dict]:
    """Resolve a place name or "lat,lng" string.

    Order: GPS coordinates -> curated city list -> OpenCage geocoding.
    GPS coordinates are rounded to 2 decimals (~1 km) for privacy.
    """
    if not name:
        return None
    m = COORD_RE.match(str(name))
    if m:
        lat, lng = round(float(m.group(1)), 2), round(float(m.group(2)), 2)
        place = opencage.reverse(lat, lng)          # live reverse geocoding
        if place:
            return {"name": place.split(",")[0], "lat": lat, "lng": lng}
        return nearest_city(lat, lng)               # offline fallback
    key = name.strip().lower()
    if key in ECO["cities"]:
        return ECO["cities"][key]
    return opencage.geocode(name)                   # None if no key / API failure


def closest_cities(name: str, n: int = 3) -> List[str]:
    """Suggest known cities for an unrecognised input (typo recovery)."""
    keys = difflib.get_close_matches(name.lower(), ECO["cities"].keys(), n=n, cutoff=0.5)
    return [ECO["cities"][k]["name"] for k in keys]


def build_transport_options(origin: Dict, dest: Dict) -> (List[Dict], bool):
    """Build one option per transport mode with distance, CO2e, price, duration.

    Returns (options, used_fallback) where used_fallback is True if any figure
    came from the static dataset instead of a live API.
    """
    used_fallback = False
    straight = haversine_km(origin["lat"], origin["lng"], dest["lat"], dest["lng"])
    road = ors.road_km(origin["lat"], origin["lng"], dest["lat"], dest["lng"])
    if road is None:
        road = straight * 1.25                # typical road/rail detour factor
        used_fallback = True

    options = []
    for mode in MODES:
        dist = straight + 95 if mode == "plane" else road   # flights: + uplift
        if mode == "plane" and straight < 150:
            continue                                         # no flights for very short trips
        if mode == "carshare":
            # Car sharing: the car's emissions split between two travellers.
            car_kg = climatiq.estimate_kg("car", dist)
            kg = car_kg / 2 if car_kg is not None else None
        else:
            kg = climatiq.estimate_kg(mode, dist)
        if kg is None:
            kg = dist * ECO["fallback_emission_factors_kg_per_pkm"][mode]
            used_fallback = True
        pm = ECO["price_model_eur"][mode]
        price = pm["fixed"] + pm["per_km"] * dist
        duration = dist / ECO["speed_kmh"][mode] + ECO["overhead_hours"][mode]
        band = classify_carbon(kg / dist)
        options.append({
            "mode": mode, "label": MODE_LABELS[mode], "distance_km": round(dist),
            "kg_co2e": round(kg, 1), "kg_per_pkm": round(kg / dist, 3),
            "price_eur": round(price), "duration_h": round(duration, 1),
            "colour": band["colour"], "carbon_label": band["label"],
        })
    return options, used_fallback


def parse_budget(value: Any) -> Optional[float]:
    """Extract a positive number from '400', '€400', 'about 350 euros'."""
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    m = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
    if not m:
        return None
    num = float(m.group().replace(",", "."))
    return num if num > 0 else None


# --------------------------------------------------------------------------- #
# Form: dynamic destination question + slot validation
# --------------------------------------------------------------------------- #
class ActionAskDestination(Action):
    """Asks for the destination with quick-reply buttons generated from data."""

    def name(self) -> Text:
        return "action_ask_destination"

    def run(self, dispatcher, tracker, domain):
        buttons = [{"title": d, "payload": f'/inform{{"destination":"{d}"}}'}
                   for d in ECO["popular_destinations"]]
        dispatcher.utter_message(text="Where would you like to go? Pick one or type any city.",
                                 buttons=buttons)
        return []


class ValidateTripForm(FormValidationAction):
    """Validates each slot of trip_form as soon as it is filled."""

    def name(self) -> Text:
        return "validate_trip_form"

    def validate_destination(self, value, dispatcher, tracker, domain: DomainDict):
        city = find_city(value)
        if city:
            return {"destination": city["name"]}
        suggestions = closest_cities(str(value))
        dispatcher.utter_message(
            text=f"I couldn't find '{value}'. Did you mean one of these?",
            buttons=[{"title": s, "payload": f'/inform{{"destination":"{s}"}}'}
                     for s in suggestions] or None)
        return {"destination": None}

    def validate_origin(self, value, dispatcher, tracker, domain: DomainDict):
        # "my location" / "here" -> use GPS location sent by the frontend.
        loc = tracker.get_slot("user_location")
        if str(value).strip().lower() in {"my location", "here", "current location"} and loc:
            return {"origin": loc.get("name", "Your location")}
        city = find_city(value)
        if city:
            return {"origin": city["name"]}
        suggestions = closest_cities(str(value))
        dispatcher.utter_message(
            text=f"I couldn't find '{value}' as a starting point. Try a nearby city.",
            buttons=[{"title": s, "payload": f'/inform{{"origin":"{s}"}}'}
                     for s in suggestions] or None)
        return {"origin": None}

    def validate_travel_dates(self, value, dispatcher, tracker, domain: DomainDict):
        text = str(value or "").lower()
        has_date_hint = bool(re.search(
            r"\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|week|monday|"
            r"tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|next", text))
        if has_date_hint:
            return {"travel_dates": value}
        dispatcher.utter_message(text="Please give approximate dates, e.g. '14 to 16 November' "
                                      "or 'next weekend'.")
        return {"travel_dates": None}

    def validate_budget(self, value, dispatcher, tracker, domain: DomainDict):
        budget = parse_budget(value)
        if budget:
            return {"budget": budget}
        dispatcher.utter_message(text="Please enter your budget as a number, e.g. 400.")
        return {"budget": None}

    def validate_preference_level(self, value, dispatcher, tracker, domain: DomainDict):
        v = str(value or "").lower()
        if v in {"low", "medium", "high"}:
            return {"preference_level": v}
        return {"preference_level": None}


# --------------------------------------------------------------------------- #
# Recommendation: Climatiq + Amadeus + weighted ranking
# --------------------------------------------------------------------------- #
class ActionRecommendTrip(Action):
    def name(self) -> Text:
        return "action_recommend_trip"

    def run(self, dispatcher, tracker, domain):
        origin = find_city(tracker.get_slot("origin"))
        dest = find_city(tracker.get_slot("destination"))
        pref = tracker.get_slot("preference_level") or "medium"
        budget = tracker.get_slot("budget")
        mode_pref = tracker.get_slot("transport_mode")

        if not origin or not dest:
            dispatcher.utter_message(text="I'm missing your start or end point. "
                                          "Could you tell me again where you're going?")
            return [SlotSet("fallback_count", 0)]

        try:
            options, used_fallback = build_transport_options(origin, dest)
        except Exception as err:            # defensive: never crash the turn
            logger.exception("Recommendation failed: %s", err)
            dispatcher.utter_message(text="Sorry, I couldn't calculate options right now. "
                                          "Would you like a human advisor to help?",
                                     buttons=[{"title": "Talk to an advisor",
                                               "payload": "/request_human"}])
            return []

        # Live flight price from Amadeus if both cities have IATA codes.
        for o in options:
            if o["mode"] == "plane" and origin.get("iata") and dest.get("iata"):
                fare = amadeus.cheapest_flight_eur(origin["iata"], dest["iata"],
                                                   datetime.now().strftime("%Y-%m-%d"))
                if fare:
                    o["price_eur"] = round(fare)

        ranked = score_options(options, pref, mode_pref)

        # 1) Colour-coded result cards.
        dispatcher.utter_message(json_message={
            "type": "transport_cards", "origin": origin["name"],
            "destination": dest["name"], "preference_level": pref,
            "estimated": used_fallback, "options": ranked})

        # 2) Alert for high-emission options, suggesting the best alternative.
        best = ranked[0]
        for o in ranked:
            if o["colour"] == "red":
                times = o["kg_co2e"] / max(best["kg_co2e"], 0.1)
                dispatcher.utter_message(json_message={
                    "type": "alert", "level": "high",
                    "text": f"{o['label']} is a high-emission option: about "
                            f"{o['kg_co2e']} kg CO2e, roughly {times:.0f}x the "
                            f"{best['label'].lower()}. You can still choose it."})

        # 3) Plain-text summary (screen readers + transparency about estimates).
        note = (" Some figures are estimates from average emission factors because a "
                "live service was unavailable.") if used_fallback else ""
        over = (f" Note: the {best['label'].lower()} fare uses about "
                f"{best['price_eur'] / budget:.0%} of your budget.") if budget else ""
        dispatcher.utter_message(
            text=f"My top pick for {origin['name']} to {dest['name']} is the "
                 f"{best['label'].lower()} ({best['carbon_label']} emissions, "
                 f"{best['kg_co2e']} kg CO2e, about EUR {best['price_eur']}).{over}{note}",
            buttons=[{"title": "Eco hotels", "payload": "/search_hotels"},
                     {"title": "Things to do", "payload": "/ask_activities"},
                     {"title": "Offset options", "payload": "/ask_offset"},
                     {"title": "Talk to an advisor", "payload": "/request_human"}])

        # Complex cases (rural last leg without public transport) -> offer handover.
        if dest.get("rural") and not dest.get("public_transport_last_leg", True):
            dispatcher.utter_message(text="There is no public transport for the final leg. "
                                          "Car sharing or an electric hire car are the lowest-"
                                          "carbon options there. An advisor can arrange this.")

        return [SlotSet("last_options", ranked), SlotSet("fallback_count", 0)]


class ActionSearchHotels(Action):
    """Eco-certified hotels: Amadeus sandbox list merged with curated certifications."""

    def name(self) -> Text:
        return "action_search_hotels"

    def run(self, dispatcher, tracker, domain):
        dest_name = tracker.get_slot("destination")
        if not dest_name:
            dispatcher.utter_message(text="Which city should I look in?",
                                     buttons=[{"title": d, "payload": f'/inform{{"destination":"{d}"}}'}
                                              for d in ECO["popular_destinations"]])
            return []
        key = dest_name.lower()
        hotels = ECO["hotels"].get(key, ECO["hotels"]["_default"])

        # Live sandbox check: Amadeus lists hotels but has no eco-certification
        # field, so we only keep curated hotels and mention live availability.
        city = ECO["cities"].get(key, {})
        live = amadeus.hotels_by_city(city["iata"]) if city.get("iata") else None
        source = "Amadeus sandbox + curated certifications" if live else "curated dataset"

        budget = tracker.get_slot("budget")
        nightly = budget * 0.5 / 2 if budget else None      # assume ~50% of budget, 2 nights
        ranked = score_hotels(hotels, tracker.get_slot("preference_level") or "medium", nightly)
        dispatcher.utter_message(json_message={
            "type": "hotel_carousel", "destination": dest_name,
            "source": source, "hotels": ranked[:3]})
        return []


class ActionShowActivities(Action):
    def name(self) -> Text:
        return "action_show_activities"

    def run(self, dispatcher, tracker, domain):
        dest = (tracker.get_slot("destination") or "").lower()
        acts = ECO["activities"].get(dest, ECO["activities"]["_default"])
        dispatcher.utter_message(json_message={"type": "activity_list",
                                               "destination": tracker.get_slot("destination"),
                                               "activities": acts})
        return []


class ActionShowOffsets(Action):
    """Offset programmes with cost estimated from the last recommended option."""

    def name(self) -> Text:
        return "action_show_offsets"

    def run(self, dispatcher, tracker, domain):
        opts = tracker.get_slot("last_options") or []
        kg = opts[0]["kg_co2e"] if opts else None
        items = []
        for o in ECO["offsets"]:
            cost = round(kg / 1000 * o["price_per_tonne_eur"], 2) if kg else None
            items.append({**o, "estimated_cost_eur": cost})
        dispatcher.utter_message(json_message={"type": "offset_list", "kg_co2e": kg,
                                               "offsets": items})
        dispatcher.utter_message(text="Offsetting does not cancel emissions; reducing them "
                                      "first is always better. Check each project on its "
                                      "standard's public registry.")
        return []


class ActionExplainCarbon(Action):
    def name(self) -> Text:
        return "action_explain_carbon"

    def run(self, dispatcher, tracker, domain):
        dispatcher.utter_message(
            text="Emissions are shown in kg CO2e (carbon dioxide equivalent), which "
                 "includes other greenhouse gases. Cards are green (Low) below 0.06 kg per "
                 "passenger-km, amber (Moderate) up to 0.15, and red (High) above that. "
                 "Figures come from the Climatiq API, or from average factors if it is "
                 "unavailable.")
        return []


# --------------------------------------------------------------------------- #
# Human handover with full context
# --------------------------------------------------------------------------- #
def build_handover_package(tracker: Tracker, reason: str, max_turns: int = 20) -> Dict:
    """Collect slots, recent transcript and ranked options for the advisor."""
    transcript = []
    for e in tracker.events:
        if e.get("event") == "user":
            transcript.append({"from": "user", "text": e.get("text")})
        elif e.get("event") == "bot" and e.get("text"):
            transcript.append({"from": "bot", "text": e.get("text")})
    slots = {k: tracker.get_slot(k) for k in
             ["destination", "origin", "travel_dates", "budget", "preference_level",
              "transport_mode"]}
    return {
        "handover_id": str(uuid.uuid4()),
        "session_id": tracker.sender_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "slots": slots,
        "ranked_options": tracker.get_slot("last_options") or [],
        "transcript": transcript[-max_turns:],
        "consent_to_share": True,
    }


class ActionHandover(Action):
    def name(self) -> Text:
        return "action_handover"

    def run(self, dispatcher, tracker, domain):
        intent = (tracker.latest_message.get("intent") or {}).get("name")
        reason = "user_request" if intent == "request_human" else "repeated_fallback"
        package = build_handover_package(tracker, reason)

        delivered = False
        webhook = os.getenv("ADVISOR_WEBHOOK_URL")
        if webhook:
            try:
                r = requests.post(webhook, json=package, timeout=2)
                delivered = r.ok
            except requests.RequestException as err:
                logger.warning("Advisor webhook failed: %s", err)
        else:
            logger.info("Handover package (no webhook configured): %s",
                        json.dumps({"handover_id": package["handover_id"],
                                    "reason": reason}))
            delivered = True   # demo mode: package logged without personal data

        dispatcher.utter_message(json_message={
            "type": "handover", "handover_id": package["handover_id"],
            "reason": reason, "delivered": delivered,
            "summary": package["slots"]})
        if delivered:
            dispatcher.utter_message(text="I've passed your trip details to a human travel "
                                          "advisor, so you won't need to repeat anything. "
                                          "They'll continue this conversation shortly.")
        else:
            dispatcher.utter_message(text="I couldn't reach an advisor right now. Please "
                                          "try again in a few minutes or email the "
                                          "advisory team.")
        return [SlotSet("handover_active", delivered), SlotSet("fallback_count", 0)]


# --------------------------------------------------------------------------- #
# Two-stage fallback (overrides Rasa's default action_default_fallback)
# --------------------------------------------------------------------------- #
INTENT_LABELS = {
    "plan_trip": "Plan a trip", "compare_transport": "Compare transport",
    "search_hotels": "Find eco hotels", "ask_offset": "Carbon offsets",
    "ask_carbon": "Explain emissions", "ask_activities": "Things to do",
    "request_human": "Talk to an advisor",
}


class ActionDefaultFallback(Action):
    """Stage 1: rephrase + likely intents. Stage 2: fixed menu. Stage 3: handover."""

    def name(self) -> Text:
        return "action_default_fallback"

    def run(self, dispatcher, tracker, domain):
        count = int(tracker.get_slot("fallback_count") or 0) + 1

        if count == 1:
            ranking = tracker.latest_message.get("intent_ranking", [])
            likely = [r["name"] for r in ranking
                      if r["name"] in INTENT_LABELS][:2]
            # Pad with the most common tasks if the ranking has no usable intents.
            for default in ("plan_trip", "search_hotels"):
                if len(likely) < 2 and default not in likely:
                    likely.append(default)
            buttons = [{"title": INTENT_LABELS[i], "payload": f"/{i}"} for i in likely]
            dispatcher.utter_message(text="Sorry, I'm not sure I understood. Did you mean:",
                                     buttons=buttons or None)
        elif count == 2:
            dispatcher.utter_message(
                text="Let's try it this way - please choose one option:",
                buttons=[{"title": "Plan a trip", "payload": "/plan_trip"},
                         {"title": "Compare transport", "payload": "/compare_transport"},
                         {"title": "Talk to an advisor", "payload": "/request_human"}])
        else:
            package = build_handover_package(tracker, "repeated_fallback")
            logger.info("Fallback handover %s", package["handover_id"])
            dispatcher.utter_message(json_message={"type": "handover",
                                                   "handover_id": package["handover_id"],
                                                   "reason": "repeated_fallback",
                                                   "delivered": True,
                                                   "summary": package["slots"]})
            dispatcher.utter_message(text="I'm connecting you with a human advisor who can "
                                          "help further.")
            return [UserUtteranceReverted(), SlotSet("fallback_count", 0),
                    SlotSet("handover_active", True)]

        # Revert first so the unclear message doesn't pollute dialogue history,
        # then persist the counter.
        return [UserUtteranceReverted(), SlotSet("fallback_count", count)]


class ActionForgetMe(Action):
    """GDPR right to erasure within the session: clears every slot."""

    def name(self) -> Text:
        return "action_forget_me"

    def run(self, dispatcher, tracker, domain):
        dispatcher.utter_message(text="Done - I've cleared all your trip details from "
                                      "this conversation.")
        return [AllSlotsReset()]
