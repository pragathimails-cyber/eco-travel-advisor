"""
tests/test_actions.py - pytest unit tests for custom actions and scoring.

All external APIs are mocked, so tests run offline and deterministically.
Run:  pytest -q tests/test_actions.py
"""
from unittest.mock import MagicMock, patch

import pytest
import requests
from rasa_sdk import Tracker
from rasa_sdk.executor import CollectingDispatcher

from actions import actions as A
from actions import api_clients as C
from actions.scoring import classify_carbon, normalise, score_hotels, score_options


def make_tracker(slots=None, intent="plan_trip", events=None, ranking=None):
    """Create a minimal Tracker with the given slots and latest intent."""
    return Tracker(
        sender_id="test-user",
        slots=slots or {},
        latest_message={"intent": {"name": intent, "confidence": 0.9},
                        "intent_ranking": ranking or [], "text": "x"},
        events=events or [],
        paused=False, followup_action=None, active_loop={}, latest_action_name=None)


# ----------------------------- scoring ------------------------------------- #
def test_classify_carbon_bands():
    assert classify_carbon(0.03)["colour"] == "green"
    assert classify_carbon(0.10)["colour"] == "amber"
    assert classify_carbon(0.25) == {"colour": "red", "label": "High"}


def test_normalise_equal_values():
    assert normalise([5, 5, 5]) == [0.0, 0.0, 0.0]


OPTS = [
    {"mode": "train", "kg_co2e": 14, "price_eur": 70, "duration_h": 4},
    {"mode": "coach", "kg_co2e": 11, "price_eur": 35, "duration_h": 7},
    {"mode": "plane", "kg_co2e": 120, "price_eur": 90, "duration_h": 4.5},
]


def test_high_preference_never_ranks_flight_first():
    ranked = score_options(OPTS, "high")
    assert ranked[-1]["mode"] == "plane"
    assert all(0 <= o["score"] <= 1 for o in ranked)


def test_preferred_mode_raises_score():
    """Stating a preferred mode increases that option's score."""
    base = {o["mode"]: o["score"] for o in score_options(OPTS, "medium")}
    pref = {o["mode"]: o["score"] for o in score_options(OPTS, "medium", "train")}
    assert pref["train"] > base["train"]


def test_score_options_empty():
    assert score_options([]) == []


def test_hotels_over_budget_penalised():
    hotels = [{"name": "A", "price_per_night": 300, "eco_tier": 3},
              {"name": "B", "price_per_night": 80, "eco_tier": 3}]
    assert score_hotels(hotels, "high", budget_per_night=100)[0]["name"] == "B"


# ----------------------------- API clients --------------------------------- #
def test_request_returns_none_on_timeout():
    with patch("actions.api_clients.requests.request",
               side_effect=requests.Timeout("slow")):
        assert C._request("GET", "https://example.org") is None


def test_climatiq_success(monkeypatch):
    monkeypatch.setenv("CLIMATIQ_API_KEY", "k")
    client = C.ClimatiqClient()
    with patch("actions.api_clients._request", return_value={"co2e": 12.3}):
        assert client.estimate_kg("train", 350) == 12.3


def test_climatiq_malformed_response(monkeypatch):
    monkeypatch.setenv("CLIMATIQ_API_KEY", "k")
    client = C.ClimatiqClient()
    with patch("actions.api_clients._request", return_value={"error": "bad"}):
        assert client.estimate_kg("train", 350) is None


def test_amadeus_without_credentials_returns_none(monkeypatch):
    monkeypatch.delenv("AMADEUS_API_KEY", raising=False)
    assert C.AmadeusClient().hotels_by_city("PAR") is None


# ----------------------------- actions ------------------------------------- #
def test_recommend_uses_fallback_when_apis_fail():
    """Climatiq + ORS fail -> options still produced and flagged as estimates."""
    d = CollectingDispatcher()
    t = make_tracker({"origin": "London", "destination": "Paris",
                      "preference_level": "high", "budget": 400})
    with patch.object(A.climatiq, "estimate_kg", return_value=None), \
         patch.object(A.ors, "road_km", return_value=None), \
         patch.object(A.amadeus, "cheapest_flight_eur", return_value=None):
        events = A.ActionRecommendTrip().run(d, t, {})
    cards = [m for m in d.messages if m.get("custom", {}).get("type") == "transport_cards"]
    assert cards and cards[0]["custom"]["estimated"] is True
    assert cards[0]["custom"]["options"][0]["mode"] in {"train", "coach"}
    assert any(m.get("custom", {}).get("type") == "alert" for m in d.messages)
    assert any(e["name"] == "last_options" for e in events)


def test_recommend_missing_slots():
    d = CollectingDispatcher()
    A.ActionRecommendTrip().run(d, make_tracker({"destination": "Paris"}), {})
    assert "missing" in d.messages[0]["text"]


def test_validate_budget_rejects_text():
    d = CollectingDispatcher()
    out = A.ValidateTripForm().validate_budget("lots", d, make_tracker(), {})
    assert out == {"budget": None}


def test_validate_destination_suggests_typo_fix():
    d = CollectingDispatcher()
    with patch.object(A.opencage, "geocode", return_value=None):
        out = A.ValidateTripForm().validate_destination("Pariss", d, make_tracker(), {})
    assert out == {"destination": None}
    assert d.messages[0]["buttons"][0]["title"] == "Paris"


def test_handover_package_contains_context():
    events = [{"event": "user", "text": "plan a trip"},
              {"event": "bot", "text": "Where to?"}]
    t = make_tracker({"destination": "Berlin", "budget": 900}, "request_human", events)
    pkg = A.build_handover_package(t, "user_request")
    assert pkg["slots"]["destination"] == "Berlin"
    assert len(pkg["transcript"]) == 2 and pkg["reason"] == "user_request"


def test_handover_webhook_failure_message(monkeypatch):
    monkeypatch.setenv("ADVISOR_WEBHOOK_URL", "https://advisor.invalid/hook")
    d = CollectingDispatcher()
    with patch("actions.actions.requests.post", side_effect=requests.ConnectionError()):
        events = A.ActionHandover().run(d, make_tracker(intent="request_human"), {})
    assert any("couldn't reach" in (m.get("text") or "") for m in d.messages)
    assert {"event": "slot", "timestamp": None, "name": "handover_active",
            "value": False} in events


@pytest.mark.parametrize("count,expected", [(0, "Did you mean"), (1, "choose one option")])
def test_two_stage_fallback(count, expected):
    d = CollectingDispatcher()
    t = make_tracker({"fallback_count": count}, "nlu_fallback",
                     ranking=[{"name": "nlu_fallback"}, {"name": "plan_trip"}])
    A.ActionDefaultFallback().run(d, t, {})
    assert expected in d.messages[0]["text"]


def test_third_fallback_escalates():
    d = CollectingDispatcher()
    t = make_tracker({"fallback_count": 2}, "nlu_fallback")
    A.ActionDefaultFallback().run(d, t, {})
    assert any(m.get("custom", {}).get("type") == "handover" for m in d.messages)
