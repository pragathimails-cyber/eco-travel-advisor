/* =============================================================================
 * app.js - React chat interface for the Eco-Travel Advisor (no JSX, no build).
 *
 * Renders Rasa REST responses:
 *   text + buttons          -> chat bubble + dynamic quick-reply buttons
 *   custom.transport_cards  -> colour-coded cards (green / amber / red)
 *   custom.hotel_carousel   -> horizontally scrolling eco-hotel carousel
 *   custom.alert            -> high-emission alert (role="alert")
 *   custom.handover         -> banner + header colour change (escalation)
 *   custom.activity_list / offset_list -> simple lists
 * Accessibility: aria-live log, text labels next to every colour, keyboard focus.
 * =========================================================================== */
const h = React.createElement;
const { useState, useRef, useEffect } = React;

// One random id per browser tab = one Rasa conversation (no cookies, no storage).
const SENDER_ID = "web-" + Math.random().toString(36).slice(2, 10);

async function sendToRasa(message) {
  // POST to the Rasa REST channel and return the list of bot messages.
  const res = await fetch(`${window.RASA_URL}/webhooks/rest/webhook`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sender: SENDER_ID, message }),
  });
  if (!res.ok) throw new Error("Rasa returned " + res.status);
  return res.json();
}

/* ---------- Custom payload renderers ---------- */
function TransportCards({ data }) {
  return h("div", { className: "cards", role: "list",
                    "aria-label": `Transport options from ${data.origin} to ${data.destination}` },
    data.options.map((o, i) => h("div", { key: o.mode, className: `card ${o.colour}`, role: "listitem" },
      h("div", { className: "top" },
        h("span", null, `${i + 1}. ${o.label}`),
        h("span", { className: `pill ${o.colour}` }, `${o.carbon_label} emissions`)),
      h("div", { className: "meta" },
        h("span", { className: "tip", title: "kg CO2e = carbon dioxide equivalent, includes other greenhouse gases" },
          `${o.kg_co2e} kg CO2e`),
        ` · €${o.price_eur} · ${o.duration_h} h · ${o.distance_km} km`),
      h("div", { className: "rank" }, `Score ${o.score} (carbon, price and your preferences)`))),
    data.estimated && h("div", { className: "note" },
      "Some figures are estimates based on average emission factors."));
}

function HotelCarousel({ data }) {
  return h("div", null,
    h("div", { className: "carousel", role: "list", "aria-label": `Eco hotels in ${data.destination}` },
      data.hotels.map(ht => h("div", { key: ht.name, className: "hotel", role: "listitem" },
        h("h4", null, ht.name),
        h("span", { className: "badge" }, ht.certification),
        h("div", { className: "meta" }, `€${ht.price_per_night}/night${ht.accessible ? " · step-free" : ""}`),
        h("div", { className: "note", title: "Why this is sustainable" }, ht.why)))),
    h("div", { className: "note" }, `Source: ${data.source}`));
}

function Custom({ data }) {
  switch (data.type) {
    case "transport_cards": return h(TransportCards, { data });
    case "hotel_carousel":  return h(HotelCarousel, { data });
    case "alert":           return h("div", { className: "alert", role: "alert" }, "⚠ " + data.text);
    case "handover":        return h("div", { className: "banner", role: "status" },
                              "👤 Escalated to a human travel advisor. Your trip details have been shared.");
    case "activity_list":   return h("div", { className: "msg bot" }, h("strong", null, "Low-impact things to do"),
                              h("ul", null, data.activities.map(a => h("li", { key: a }, a))));
    case "offset_list":     return h("div", { className: "msg bot" }, h("strong", null, "Verified offset programmes"),
                              h("ul", null, data.offsets.map(o => h("li", { key: o.name },
                                `${o.name} - ${o.standard}` + (o.estimated_cost_eur != null ? ` (≈ €${o.estimated_cost_eur})` : "")))));
    default: return null;
  }
}

/* ---------- Main app ---------- */
function App() {
  const [msgs, setMsgs] = useState([]);
  const [input, setInput] = useState("");
  const [handover, setHandover] = useState(false);
  const logRef = useRef(null);

  useEffect(() => { logRef.current && (logRef.current.scrollTop = logRef.current.scrollHeight); }, [msgs]);
  useEffect(() => { send("/greet", "") }, []);   // start with the greeting + privacy notice

  async function send(payload, display) {
    if (display) setMsgs(m => [...m, { from: "user", text: display }]);
    try {
      const replies = await sendToRasa(payload);
      replies.forEach(r => { if (r.custom && r.custom.type === "handover") setHandover(true); });
      setMsgs(m => [...m, ...replies.map(r => ({ from: "bot", ...r }))]);
    } catch (e) {
      // Network/server failure: tell the user instead of failing silently.
      setMsgs(m => [...m, { from: "bot", text: "Sorry, I can't reach the server right now. Please try again." }]);
    }
  }

  function onSubmit(e) {
    e.preventDefault();
    if (!input.trim()) return;
    send(input, input); setInput("");
  }

  function shareLocation() {
    // GPS: coordinates rounded to ~1 km before leaving the browser.
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(pos => {
      const c = `${pos.coords.latitude.toFixed(2)},${pos.coords.longitude.toFixed(2)}`;
      send(`/inform{"origin":"${c}"}`, "📍 Shared my location");
    });
  }

  function voiceInput() {
    // Optional voice mode using the browser SpeechRecognition API.
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { alert("Voice input is not supported in this browser."); return; }
    const rec = new SR(); rec.lang = "en-GB";
    rec.onresult = ev => { const t = ev.results[0][0].transcript; send(t, t); };
    rec.start();
  }

  return h("div", { className: "app" },
    h("header", { className: handover ? "handover" : "" },
      h("span", { "aria-hidden": true }, handover ? "👤" : "🌿"),
      h("h1", null, "Eco-Travel Advisor"),
      h("span", { className: "status" }, handover ? "Human advisor" : "Virtual assistant")),
    handover && h("div", { className: "banner", role: "status" }, "You are now connected to a human travel advisor."),
    h("div", { className: "log", ref: logRef, "aria-live": "polite", "aria-label": "Conversation" },
      msgs.map((m, i) => h(React.Fragment, { key: i },
        m.text && h("div", { className: `msg ${m.from}` }, m.text),
        m.custom && h(Custom, { data: m.custom }),
        // Quick replies are only shown while they are still relevant
        // (i.e. the user has not replied since this bot message).
        m.buttons && msgs.slice(i + 1).every(x => x.from === "bot")
          ? h("div", { className: "quick" }, m.buttons.map(b =>
              h("button", { key: b.title, onClick: () => send(b.payload, b.title) }, b.title)))
          : null))),
    h("form", { className: "composer", onSubmit },
      h("label", { htmlFor: "msg", className: "sr-only" }, "Type your message"),
      h("input", { id: "msg", value: input, placeholder: "Type a message…", autoComplete: "off",
                   onChange: e => setInput(e.target.value) }),
      h("button", { type: "button", className: "icon", onClick: shareLocation, "aria-label": "Share location" }, "📍"),
      h("button", { type: "button", className: "icon", onClick: voiceInput, "aria-label": "Voice input" }, "🎤"),
      h("button", { type: "submit" }, "Send")));
}

ReactDOM.createRoot(document.getElementById("root")).render(h(App));
