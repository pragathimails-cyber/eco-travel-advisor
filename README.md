# Rasa Eco-Travel Advisor 🌿

A conversational agent that helps travellers plan lower-carbon trips. It asks
about destination, dates, budget and sustainability preference, estimates
CO2e per transport mode (Climatiq), fetches hotel and flight data (Amadeus
sandbox), ranks options with a weighted carbon/price/preference score, shows
colour-coded results and hands complex cases to a human advisor with full
context.

Built with **Rasa Open Source 3.6** (NLU + Core), a **Python action server**
and a **React** chat UI using the Rasa REST channel.

---

## Repository structure

```
config.yml               NLU pipeline (spaCy + DIET) and policies
config_distilbert.yml    Optional DistilBERT pipeline (LanguageModelFeaturizer)
domain.yml               Intents, entities, slots, form, responses, actions
data/nlu.yml             NLU training data (v2, augmented)
data/rules.yml           Rules incl. fallback and handover
data/stories.yml         Multi-turn stories
actions/actions.py       Custom actions (recommendation, hotels, handover, fallback ...)
actions/api_clients.py   Climatiq, Amadeus, OpenRouteService, OpenCage clients
actions/scoring.py       Carbon colour bands and weighted ranking (pure functions)
actions/data/eco_data.json  Curated eco hotels, activities, offsets, fallback factors
tests/test_stories.yml   Dialogue tests (rasa test core)
tests/test_actions.py    Pytest unit tests with mocked APIs
frontend/                React chat UI (no build step; React vendored locally)
docker-compose.yml       Rasa :5005, actions (internal), frontend :3000
deploy/hf/               Single-container build for HuggingFace Spaces
scripts_dev_tunnel.py    Pyngrok HTTPS tunnel for development
results/                 Evaluation outputs used in the report
```

## 1. Quick start with Docker (recommended)

```bash
git clone <your-repo-url> eco-travel-advisor
cd eco-travel-advisor
cp .env.example .env          # add your API keys (optional - see below)
docker compose up --build
```
Open **http://localhost:3000**.

API keys are optional: without them every action falls back to the curated
dataset and tells the user that figures are estimates.

## 2. Local development (without Docker)

Rasa 3.6 needs **Python 3.10**.

```bash
python3.10 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_md-3.4.1/en_core_web_md-3.4.1-py3-none-any.whl
export ACTION_ENDPOINT_URL=http://localhost:5055/webhook
set -a; source .env; set +a  # load API keys

rasa train                                   # ~3 min on one CPU
rasa run actions --actions actions           # terminal 1 (port 5055)
rasa run --enable-api --cors "*"             # terminal 2 (port 5005)
python -m http.server 3000 -d frontend       # terminal 3 -> http://localhost:3000
```

## 3. API keys (`.env`)

| Variable | Where to get it | Free tier |
|---|---|---|
| `CLIMATIQ_API_KEY` | climatiq.io dashboard | free monthly calls |
| `AMADEUS_API_KEY` / `AMADEUS_API_SECRET` | Amadeus for Developers (test env) | sandbox |
| `ORS_API_KEY` | openrouteservice.org | daily quota |
| `OPENCAGE_API_KEY` | opencagedata.com | daily quota |
| `ADVISOR_WEBHOOK_URL` | your helpdesk / Slack webhook | optional |

`.env` is in `.gitignore` - never commit keys. Climatiq activity IDs can be
overridden with `CLIMATIQ_ID_TRAIN`, `CLIMATIQ_ID_COACH`, `CLIMATIQ_ID_CAR`,
`CLIMATIQ_ID_PLANE`; confirm them in the Climatiq Data Explorer.

## 4. Testing / replicating the results

```bash
# Unit tests (no network needed - APIs are mocked)
pytest -v tests/test_actions.py

# NLU hold-out test (80/20 split, fixed seed)
rasa data split nlu --training-fraction 0.8 --random-seed 42
rasa train nlu --nlu train_test_split/training_data.yml --out models/split
rasa test nlu -m models/split/<model>.tar.gz --nlu train_test_split/test_data.yml

# NLU cross-validation
rasa test nlu --cross-validation --folds 3 --nlu data/nlu.yml

# Dialogue tests
rasa test core --stories tests/test_stories.yml

# Training curves (DIET writes TensorBoard logs)
tensorboard --logdir results/tensorboard
```
Reports, confusion matrices and histograms are written to `results/`.

## 5. Deploying to HuggingFace Spaces

1. Create a new Space -> **Docker** SDK.
2. Push this repository and set `deploy/hf/Dockerfile` as the Space Dockerfile
   (copy it to the repository root as `Dockerfile`).
3. Add API keys under *Settings -> Variables and secrets*.
4. The Space serves the UI and proxies `/webhooks/*` to Rasa on port 7860.

## 6. Development tunnel (Pyngrok)

```bash
pip install pyngrok
export NGROK_AUTHTOKEN=...
python scripts_dev_tunnel.py      # prints an https URL for the Rasa server
```

## Privacy

In-memory tracker store (nothing persisted), GPS rounded to ~1 km, consent
notice at the start, "forget me" clears all slots, logs exclude personal data.

## Data disclaimer

Hotel, activity and offset entries in `eco_data.json` are illustrative sample
data. Verify every property against the certification scheme's public register
before production use.
