# Quietlog

An **agentic migraine diary**: tap **Log attack**, speak for ten seconds — or just
tell the assistant — and get a **one-page printable summary** for your next
appointment. Quietlog's agent keeps the diary for you; you only confirm it.

**The problem.** Doctors ask people with migraines to keep a diary, but the worst
moment to write one is during an attack — exactly when the data is most valuable.
People reconstruct it from memory days later, or not at all.

**What Quietlog does.** It records and organizes what the user says: a voice note
or typed text is transcribed, structured into diary fields, shown next to the
user's own words for confirmation, and counted into a printable summary. Attacks
can also be logged with a start/end timer, from a chat-style assistant, or with a
single tap ("no headache today"). Log-time weather is snapped alongside — and
when processing fails, the app quietly keeps retrying in the background instead
of asking you to redo anything.

**What Quietlog never does.** It does not diagnose, interpret, or advise. It never
generates medical advice, suggests triggers to avoid, recommends or comments on
medication, or judges patterns. The only model output in the product is structured
extraction from the user's own words plus tightly bounded assistant tool calls.
The one piece of model-written prose — an optional appointment paragraph the
user drafts, reviews, and chooses to include — is labeled as AI-drafted in the
PDF.

> Quietlog records what you tell it. It does not give medical advice. If this feels
> like an emergency, contact your local emergency services.

## Meet the agent

Quietlog is built around an assistant agent, not a form. Open it from the
floating button and just say what happened — *"bad one since lunch, took
Excedrin"* — or tap a quick action and it acts:

- **Logs for you.** Starts/stops attack timers, saves voice and typed notes,
  records no-headache days, attaches notes to the right attack, corrects an
  entry's date, and answers its own follow-up questions into the entry.
- **Asks, doesn't guess.** When "that one" could mean two entries, it asks one
  clarifying question. It answers questions about your diary ("how many
  attacks this month?") from your actual records, and drafts the appointment
  summary paragraph from the computed numbers.
- **Remembers safely.** Across turns it tracks the running attack, the entry
  being discussed, and pending follow-ups as structured state, revalidated
  against the database every turn — never trusting ids echoed in chat text.
- **Receipts, not vibes.** Every confirmation is generated from the actual
  tool result; a failed write never reports "saved." Requests and recordings
  carry stable ids, so a lost response replays the original result instead
  of double-logging.
- **Works while you don't.** Transcription and extraction run save-first and
  recover automatically — bounded retries with backoff, resumed after app
  restarts and screen changes. Common actions (start/stop timer, check-in)
  skip the model entirely and run instantly.

The agent's tools are validated server-side with strict argument schemas, and
its boundaries are enforced in code, not just prompting: no diagnoses, no
treatment advice, no invented details, and confirmed history it cannot touch.

## Features

- **Capture your way.** Voice notes (automatic transcription, transcripts persist
  the moment they arrive), typed notes, a running attack timer (start → add notes
  → end), and one-tap no-headache check-ins.
- **Chat assistant.** The floating agent above — log, ask, and manage your
  diary conversationally, with quick actions that skip the model entirely.
- **Review, don't trust.** Every extracted field sits next to the user's words;
  entries stay *unconfirmed* until reviewed. Confirmed history is closed — the
  assistant can't append to it or re-extract it.
- **History, Insights, Reports.** Month-grouped history, a monthly dashboard
  (attack days, average pain, logged days, interactive calendar), 90-day
  insights, and a printable one-page summary (30/60/90-day windows) shareable as
  PDF.
- **Weather, fail-soft.** Temperature, humidity, and pressure tendency are
  captured at log time (and backfilled for backdated attacks). No weather never
  blocks a save.
- **Recovery without re-recording.** Transcription/extraction failures retry
  with bounded backoff; pending voice work resumes after app restarts and
  screen changes. A failed recording is always retryable as-is — you never have
  to re-record because transcription hiccupped.

## What was built during the hackathon

Built for the Meta Global AI Developer Hackathon. All code was written during the
hackathon; no third-party code was copied. The agentic assistant — bounded tool
loop, idempotent turns, honest receipts — was designed and hardened against a
multi-turn evaluation suite, not bolted on.

- **M1 (complete):** data model, `schema/fields.yaml`, synthetic diary generator
  with planted aggregates, deterministic summary calculations, unit tests.
- **M2 (complete):** transcription + extraction services with response cache,
  quote verifier + one repair attempt, `scripts/evaluate.py`. Test 1: 92.0%
  micro accuracy over 66 transcripts, 0% hallucination before and after the
  verifier; corrupted-output rejection 12/12; planted-aggregate match 4/4.
  Full numbers in `reports/evaluation.md` (regenerable; not committed).
- **M3 (complete, Expo pivot):** capture + review UI as an Expo mobile app
  (TypeScript + React Native, SDK 58, runs in Expo Go), mic PCM capture with
  in-app conversion to 16 kHz mono WAV, save-first API with retry queue.
  Bottom tabs (Home, History, Insights, Reports) with a floating assistant.
- **M4 (complete):** `GET /summary` endpoint, in-app summary screen
  (30/60/90-day windows, calendar grid, all counts), one-page B/W print HTML
  via `expo-print` + share sheet.
- **Post-M4 (complete):** assistant reliability hardening (idempotent requests,
  strict tool validation, honest write receipts, ambiguous-target clarification),
  separated transcription/extraction with per-recording state and crash-safe
  recovery, and a bounded live-model multi-turn evaluation
  (`scripts/live_agent_eval.py`, isolated synthetic databases only).

Current verification: **265 `pytest` passing**, TypeScript (`tsc --noEmit`)
clean, iOS bundle (`expo export`) clean. On-device verification of the newest
flows is ongoing.

## Architecture

```
Quietlog/
  schema/fields.yaml          canonical field list (record-keeping schema)
  src/quietlog/
    models.py                 Pydantic data model (entries, attacks, recordings)
    db.py                     SQLite persistence (stdlib sqlite3)
    time_resolve.py           resolves stated time expressions, no date arithmetic by the model
    summary.py                deterministic summary calculations (no model involved)
    main.py                   FastAPI app factory
    routes.py                 capture/review/timer/transcribe endpoints (save-first)
    processing.py             transcribe → extract → verify pipeline, overlap-safe
    settings.py               server settings from environment
    llm_service.py            Muse Spark structured extraction + response cache
    transcribe_service.py     Muse Voice Transcribe + response cache
    verifier.py               deterministic quote verifier + repair step
    agent.py                  bounded assistant tool loop (validated args, retries-safe)
    assist.py                 ask-your-diary + narrative drafting over stored entries
    weather_service.py        Open-Meteo snapshots (fail-soft, no key)
    cache.py                  response cache helpers
  scripts/
    make_synthetic_diary.py   synthetic 90-day diary + 66 labelled transcripts
    evaluate.py               one-command evaluation, writes a Markdown report
    live_agent_eval.py        bounded live-model multi-turn eval (isolated DBs)
    seed_demo.py              synthetic Sep/Oct demo entries
  data/synthetic/             committed synthetic fixtures (diary, aggregates, transcripts)
  frontend/                   Expo mobile app (TypeScript + React Native, SDK 58)
    src/screens/              Home, Capture, History, Insights, Reports, Assistant
    src/api.ts                typed backend client (EXPO_PUBLIC_API_URL)
    src/voice.ts              voice recovery driver (bounded retries, background sweep)
    src/audio.ts              mic PCM stream → 16 kHz mono 16-bit WAV, in-app
    src/theme.ts              calm warm dark palette (no pure black/white, one amber)
    src/chat.tsx              assistant transcript + retry-safe request state
    src/print.ts              one-page B/W print HTML for expo-print
```

Key design decisions:

- **Save first, process later.** Capture writes audio + text immediately and
  shows "Saved"; transcription/extraction run afterward and can be retried.
  No entry is ever lost to a model or network failure.
- **Transcription ≠ extraction.** Transcripts are persisted per recording the
  moment they arrive; extraction failure never costs a retranscription, and
  repeated processing never duplicates text. Failure kinds are explicit:
  transient (bounded backoff, auto-retry) vs permanent (invalid audio/auth).
- **The model copies; code computes.** The model returns time expressions
  verbatim ("since 2pm") and durations as stated; backend code resolves
  datetimes and the verifier checks numbers against quotes. The model never
  does date arithmetic.
- **Day attribution.** Each entry belongs to the local calendar day it was
  saved on (`entry_date`). A day with no entry and no check-in is **not
  logged**, never "no attack".
- **Symptoms are tri-state.** True = stated present, False = explicitly denied
  ("no nausea this time"), None = not mentioned. The summary counts only True:
  "mentioned in X of N attack entries". Silence is not absence.
- **Medications group by normalized name** (case/whitespace-insensitive) for
  counting, displaying the first-seen form. Brand vs generic ("Advil" vs
  "ibuprofen") are deliberately NOT merged — merging would be interpretation.
- **Severity 1/2/3 is set only from function/severity statements** (can
  function / activities slowed / cannot do activities). A stated 0–10 number
  is stored separately as `pain_0_10` and never converted.
- **Assistant requests are idempotent.** A client-generated request id per
  message makes retries (including simultaneous ones) return the stored
  result; reused ids with different input are rejected. Recordings carry
  their own stable ids for the same guarantee. Editing fields by hand marks
  them so re-extraction never overwrites user corrections.
- **SQLite via stdlib `sqlite3`**, no ORM, so Windows setup stays trivial.

## Setup (Windows PowerShell)

```powershell
# From the repository root
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Generate the synthetic fixtures
python scripts\make_synthetic_diary.py

# Run the test suite
python -m pytest

# Start the API. Loopback for simulators:
uvicorn quietlog.main:app --host 127.0.0.1 --port 8000
# ...or your LAN IP for a real phone (the phone is not 127.0.0.1 to itself):
uvicorn quietlog.main:app --host 0.0.0.0 --port 8000

# In a second shell, start the app
cd frontend
npm install
# Point the app at the API (copy .env.example to .env first on a real
# device, with your PC's LAN IP, e.g. http://192.168.1.20:8000)
npx expo start
# Scan the QR code with Expo Go on your phone.
```

## Environment variables

Copy `.env.example` to `.env` (never commit `.env`). The backend loads
`.env` automatically on startup; real environment variables win over it:

| Variable | Purpose |
|---|---|
| `QUIETLOG_MODEL_BASE_URL` | Meta Model API base URL |
| `QUIETLOG_SPARK_MODEL` | Muse Spark model identifier |
| `QUIETLOG_VOICE_MODEL` | Muse Voice Transcribe identifier |
| `QUIETLOG_MODEL_API_KEY` | API key |
| `QUIETLOG_MODEL_TIER` | Keep `standard` (see below) |
| `QUIETLOG_HOST` / `QUIETLOG_PORT` | Server bind (default `127.0.0.1:8000`) |
| `QUIETLOG_DB` / `QUIETLOG_AUDIO_DIR` / `QUIETLOG_CACHE_DIR` | Local paths |
| `QUIETLOG_WEATHER` / `QUIETLOG_WEATHER_TIMEOUT` | Weather snapshots on/off + timeout |
| `EXPO_PUBLIC_API_URL` | (frontend) API base URL the app talks to |

**Model tier.** Default to the **standard** (non-contributor) Muse Spark tier.
The contributor tier lets Meta train on prompts and **must not be used with
real health information**. Quietlog refuses contributor-named models in code.

## Meta Model API integration

- **Base URL:** `https://api.meta.ai/v1`, OpenAI-compatible
  (`/chat/completions`). Auth is `Authorization: Bearer <key>`.
- **Extraction:** `muse-spark-1.3` (standard tier, not used for training).
  Quietlog requests `response_format: json_schema`, falls back to
  `json_object`, then parses raw JSON — every field is then checked by the
  deterministic quote verifier before it is stored.
- **Assistant:** the same model drives a bounded tool loop with strict,
  server-validated arguments; confirmations are generated from actual tool
  results, never from the model's prose. A bounded live-model comparison
  against `muse-wonder-1.0` (synthetic multi-turn suite, tool choices + DB
  state asserted) found no advantage, so the default remains
  `muse-spark-1.3` — switchable any time via `QUIETLOG_SPARK_MODEL`.
- **Transcription:** `muse-voice-transcribe-1.0` via one-shot
  `POST /asr/transcribe` (multipart: JSON `request` part + `audio` file
  part). Limits: 32 MB body, 10 minutes of audio.
- **Audio format:** WAV container. The engine is native at 24 kHz mono
  16-bit; 16 kHz is accepted. The app converts recordings to WAV in-app so
  Windows needs no ffmpeg.
- **Tiers:** standard (`muse-spark-1.3`) vs contributor
  (`muse-spark-1.3-contributor` — Meta trains on contributor prompts and
  completions). Quietlog refuses contributor models in code; see
  `.env.example`.

Any model failure leaves the entry saved with status `needs_processing` for
retry; process failures never lose audio or text.

## Field definitions and sources

The field list follows published headache diaries — The Migraine Trust's and
Migraine Canada's migraine diaries (see the header comment in
[schema/fields.yaml](schema/fields.yaml)) — and claims no clinical
validation. All fields are nullable; "unknown" is always valid. The canonical
definitions live in `schema/fields.yaml`, enforced by
`src/quietlog/models.py`.

## The quote verifier

Every non-null extracted field carries an `evidence_quote` copied from the
transcript. Deterministic code (`src/quietlog/verifier.py`) normalizes
case/whitespace/punctuation and requires each quote to be a substring of the
transcript; `stated_as` values (names, doses, times, triggers) must appear
inside their own quote; stated numbers must match the quote (digits or
English number words with a matching unit, e.g. "two hours" → 120 minutes,
"7 out of 10" → 7); and symptom true/false values must agree with negation
wording ("no nausea" cannot support `nausea: true`). Failing fields reset to
unknown and are flagged "not supported by what you said". One targeted
repair attempt per extraction, then re-verify.

## How the summary is computed

`src/quietlog/summary.py:compute_summary` takes stored entries and a window
(30/60/90 days, chosen by the user) and returns every number on the printed
page:

- Day counts: days with an attack entry, days with a "no attack" check-in,
  days not logged, unconfirmed entries. A day with both an attack and a
  check-in counts in both; the calendar marks it as an attack day.
- Calendar grid data: attack / no attack / not logged per day, plus an
  unconfirmed marker. Shapes and symbols, never color alone.
- Duration median/range among entries that have a duration; severity counts
  1/2/3; weather coverage counts.
- Symptoms as "mentioned in X of N attack entries"; medications per name
  (attacks taken for, distinct days, outcome counts); triggers as stated
  with counts and N.

**Why "not logged" is not "no attack".** An attack diary only knows what was
recorded. Treating silent days as attack-free would undercount attacks and
mislead the appointment conversation, so unlogged days are reported as
unknown.

## Evaluation

```powershell
python scripts\make_synthetic_diary.py   # regenerate fixtures (fixed seeds)
python scripts\evaluate.py               # writes reports/evaluation.md
python scripts\live_agent_eval.py --stub # assistant eval harness self-test (no network)
python scripts\live_agent_eval.py        # bounded live-model multi-turn eval (isolated DBs, never production)
```

M2 results (`muse-spark-1.3`, 66 transcripts, full table in
`reports/evaluation.md`): micro accuracy 92.0% (971/1056 field slots);
hallucination 0/307 before and 0/306 after the verifier (every model quote
was transcript-supported, so repair never triggered live). Strongest fields:
duration, pain scale, symptoms, medications (93.9%). Known gaps are
prompt/scoping follow-ups rather than verifier failures: `ongoing` 28.8%
(model abstains where labels infer from tense), severity 77.3% (bare
"mild"/"severe" without a function statement), time expressions 77.3%
(some "misses" are equivalent longer spans). Verifier rejection 12/12;
summary matches planted aggregates on the fixtures plus seeds 1, 7, 99.

The assistant eval (`scripts/live_agent_eval.py`) checks multi-turn tool
choices and final database state — timer → pain → medication → end,
ambiguous targets, date corrections, "never mind" after saving, and lost-
response retries — on isolated synthetic databases with a hard call budget.
Production diary data is never touched.

What these tests do and do not show: synthetic evaluation tests the code,
not real patients or real speech variety.

## Privacy

- All user data stays local (SQLite + audio files on the user's machine).
- The repository contains **only synthetic data** (`data/synthetic/`). Real
  diary entries, audio, `.env`, and local databases are gitignored and
  never committed.
- Standard model tier only (see above); contributor models are refused in
  code.

## Limitations

- Not clinically validated; not medical advice.
- Speech recognition errors are possible; every extracted field is shown
  next to its quote for user confirmation, and unconfirmed entries are
  marked.
- Single user, single device; no accounts, no cloud sync (cut by design).
- Synthetic evaluation only (see above); live-model comparisons are small
  (single runs per scenario), so treat model-choice conclusions as
  provisional.
- `attack_start` resolution covers common phrasings (clock times, relative
  offsets, dayparts, meals, bare days); anything else stays unknown rather
  than guessed.
- Each medication carries a single outcome; when efficacy and side effects
  are both reported, the efficacy outcome is kept and the rest goes to
  notes.
- Background recovery assumes a single backend worker; simultaneous
  multi-process writes to the same entry are serialized best-effort.
