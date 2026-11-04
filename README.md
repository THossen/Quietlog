# Quietlog

An **agentic migraine diary**: tap **Log attack**, speak for ten seconds — or just
tell the assistant — and get a **one-page printable summary** for your next
appointment. Quietlog's agent keeps the diary for you; you only confirm it.

## Built with

| Layer | Choice | Used for |
|---|---|---|
| Agent + extraction | Muse Spark 1.3 | Bounded tool loop, structured fields |
| Voice | Muse Voice Transcribe 1.0 | Voice-note transcription |
| Backend | Python 3.11 · FastAPI · Pydantic 2 | Save-first API, validated tools |
| Storage | SQLite (Python stdlib) | Local-only diary, no ORM |
| Mobile | Expo SDK 58 · React Native · TypeScript | Expo Go app, no dev build |
| Native features | expo-audio · expo-location · expo-print/sharing | Mic, weather point, PDF |
| Weather | Open-Meteo | Keyless log-time snapshots |
| Quality | pytest · multi-turn live-model eval | Synthetic-only verification |

## Contents

- [Quick start](#quick-start) · [Meet the agent](#meet-the-agent) · [Features](#features) · [Try it locally](#try-it-locally--mac--windows)
- [Architecture](#architecture) · [Meta Model API integration](#meta-model-api-integration) · [Evaluation](#evaluation)
- [Privacy](#privacy) · [Limitations](#limitations) · [License](#license)

## Quick start

Two terminals — backend, then app. Full Mac & Windows walkthrough with
simulator, emulator, and real-phone options: [Try it locally](#try-it-locally--mac--windows).

1. **Backend:** create `.venv`, `pip install -e ".[dev]"`, copy
   `.env.example` to `.env` and set `QUIETLOG_MODEL_API_KEY`, then
   `uvicorn quietlog.main:app --host 0.0.0.0 --port 8000` (use
   `127.0.0.1` instead for simulator-only testing).
2. **App:** `cd frontend`, `npm install`, copy `.env.example` to `.env`
   and set `EXPO_PUBLIC_API_URL` for your testing target, then
   `npx expo start -c` and scan the QR code with Expo Go.

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

**How a note flows:** speak or type → saved instantly → transcribed →
extracted into fields → every field checked against your words by the quote
verifier → shown for your review → counted into the summary/PDF. Weather is
snapped at log time; nothing in that chain can lose what you saved.

### What the agent can do

Fourteen validated tools, dispatched from what you say:

| You say | Tool(s) | Result |
|---|---|---|
| "Bad one since lunch, took Excedrin" | `log_attack` | Note saved + fields extracted |
| "No headache today" | `log_checkin` | No-headache day recorded |
| "Also, I took ibuprofen at noon" | `add_note` | Appended to the same attack |
| "Start a timer" / "It's over" | `timer_start` / `timer_end` / `timer_status` | Attack timed live |
| "Pain was 7 out of 10" (follow-up) | `answer_followups` | Answer folded into the entry |
| "How many attacks this month?" | `answer_diary` / `get_summary` | Answered from your records |
| "Draft my appointment summary" | `draft_narrative` | Paragraph from computed numbers |
| "Save that as a PDF" | `save_pdf` | One-page PDF + share sheet |
| "Find last Tuesday's headache" | `find_entries` / `get_entry` | Matching entry shown |
| "Actually, that was yesterday" | `set_entry_date` | Whole attack moved to that day |

## Features

- **Capture your way.** Voice notes (automatic transcription, transcripts persist
  the moment they arrive), typed notes, a running attack timer (start → add notes
  → end), and one-tap no-headache check-ins.
- **Review, don't trust.** Every extracted field sits next to the user's words;
  entries stay *unconfirmed* until reviewed. Confirmed history is closed — the
  assistant can't append to it or re-extract it.
- **History, Insights, Reports.** Month-grouped history, a monthly dashboard
  (attack days, average pain, logged days, interactive calendar), 90-day
  insights, and a printable one-page summary (30/60/90-day windows) shareable as
  PDF.
- **Weather, fail-soft.** Temperature, humidity, and pressure tendency are
  captured at log time (and backfilled for backdated attacks). Missing
  weather never blocks a save.
- **Recovery without re-recording.** Transcription/extraction failures retry
  with bounded backoff; pending voice work resumes after app restarts and
  screen changes. A failed recording is always retryable as-is — you never have
  to re-record because transcription hiccupped.

## What was built during the hackathon

Built for the Meta Global AI Developer Hackathon. All code was written during the
hackathon; no third-party code was copied. The agentic assistant — bounded tool
loop, idempotent turns, honest receipts — was designed and hardened against a
multi-turn evaluation suite, not bolted on.

<details>
<summary>Milestone history (M1–M4 + post-M4 hardening)</summary>

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

Current verification: **266 `pytest` passing**, TypeScript (`tsc --noEmit`)
clean, iOS bundle (`expo export`) clean. On-device verification of the newest
flows is ongoing.

</details>

## Architecture

```
Quietlog/
  schema/fields.yaml          canonical field list (record-keeping schema)
  src/quietlog/               FastAPI backend (Python)
    agent.py                  bounded assistant tool loop (validated args, retries-safe)
    processing.py             transcribe → extract → verify pipeline, overlap-safe
    routes.py                 capture/review/timer/transcribe endpoints (save-first)
    llm_service.py            Muse Spark structured extraction + response cache
    transcribe_service.py     Muse Voice Transcribe + response cache
    verifier.py               deterministic quote verifier + repair step
    summary.py                deterministic summary calculations (no model involved)
    db.py · models.py         SQLite persistence + Pydantic data model
  scripts/                    synthetic fixtures, evaluation, live-model eval
  data/synthetic/             committed synthetic fixtures (diary, aggregates, transcripts)
  frontend/                   Expo mobile app (TypeScript + React Native, SDK 58)
    src/screens/              Home, Capture, History, Insights, Reports, Assistant
    src/api.ts · voice.ts     typed backend client · voice recovery driver
    src/audio.ts · print.ts   mic → 16 kHz mono WAV in-app · one-page print HTML
```

The field list follows published headache diaries — The Migraine Trust's and
Migraine Canada's migraine diaries (see the header comment in
[schema/fields.yaml](schema/fields.yaml)) — and claims no clinical
validation. All fields are nullable; "unknown" is always valid. The canonical
definitions live in `schema/fields.yaml`, enforced by
`src/quietlog/models.py`.

Key design decisions:

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

## Try it locally — Mac & Windows

Quietlog runs the same way on both platforms: a Python/FastAPI backend on
your computer plus the Expo app in Expo Go (real phone) or a simulator/
emulator. The only differences are the virtualenv activation path and how
you find your LAN IP — both are covered below. No build step and no
ffmpeg is needed on either platform.

**Prerequisites (both platforms):**

- Python **3.11+** — Windows: `python --version`; Mac: `python3 --version`
  (if `python3` is missing, install from python.org or `brew install python@3.11`)
- Node.js **20 LTS or newer** with npm — `node --version`
- [Expo Go](https://expo.dev/go) on your phone (iOS or Android), on the
  **same Wi-Fi** as the computer for real-device testing

### 1. Backend

From the repository root, in a first terminal:

**Windows (PowerShell):**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
# If activation is blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install -e ".[dev]"
Copy-Item .env.example .env   # then edit .env and set QUIETLOG_MODEL_API_KEY
python scripts/make_synthetic_diary.py
python -m pytest
```

**Mac (Terminal, zsh/bash):**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env          # then edit .env and set QUIETLOG_MODEL_API_KEY
python scripts/make_synthetic_diary.py
python -m pytest
```

Then start the API (same command on both platforms, venv activated):

```bash
# Simulator/emulator on the same computer:
uvicorn quietlog.main:app --host 127.0.0.1 --port 8000
# Real phone via Expo Go (phone reaches the computer over Wi-Fi):
uvicorn quietlog.main:app --host 0.0.0.0 --port 8000
```

Sanity-check it before opening the app: <http://127.0.0.1:8000/health>
should return `{"status":"ok"}` (interactive docs at `/docs`).

### 2. Frontend

In a **second** terminal (no venv needed):

**Windows (PowerShell):**

```powershell
cd frontend
npm install
Copy-Item .env.example .env   # then set EXPO_PUBLIC_API_URL as below
npx expo start -c
```

**Mac (Terminal):**

```bash
cd frontend
npm install
cp .env.example .env          # then set EXPO_PUBLIC_API_URL as below
npx expo start -c
```

`-c` clears the Metro cache — required after changing `.env`, because
`EXPO_PUBLIC_*` values are inlined when the bundle is built.

### 3. Point the app at your backend

Edit `frontend/.env`. Pick the row for how you are testing:

| Testing on | `EXPO_PUBLIC_API_URL` | Backend `--host` |
|---|---|---|
| iOS Simulator (Mac only) | `http://127.0.0.1:8000` | `127.0.0.1` |
| Android Emulator (Mac & Windows) | `http://10.0.2.2:8000` | `127.0.0.1` |
| Real phone, Expo Go (Mac & Windows) | `http://<your-LAN-IP>:8000` | `0.0.0.0` |

A real phone is never `127.0.0.1` to itself — use the computer's LAN
address, e.g. `http://192.168.1.20:8000`. Find it with:

- **Windows:** run `ipconfig` and use the **IPv4 Address** under your Wi-Fi adapter.
- **Mac:** run `ipconfig getifaddr en0` (Wi-Fi), or check System Settings → Wi-Fi → Details.

Then restart Expo (`Ctrl+C`, then `npx expo start -c`) so the new URL is picked up.

### 4. Open the app

- **Real phone:** scan the QR code printed by `npx expo start` with Expo Go
  (Android) or the Camera app (iOS). Keep phone and computer on the same Wi-Fi.
- **iOS Simulator (Mac only):** press `i` in the Expo terminal.
- **Android Emulator (Mac & Windows):** press `a` in the Expo terminal
  (emulator must already be running).
- **No phone handy:** press `w` for web — useful as a smoke check only;
  mic, location, and print/share behave differently than on device.

**Connection troubleshooting ("Cannot reach Quietlog"):** confirm
`http://127.0.0.1:8000/health` loads on the computer; confirm
`EXPO_PUBLIC_API_URL` matches the table above and Expo was restarted with
`-c`; on Windows allow Python/Node through Windows Defender Firewall on
**Private** networks, on Mac check System Settings → Network → Firewall
for Python/Node; and disable VPNs or router AP/client isolation, which
block phone-to-computer LAN traffic.

## Environment variables

Copy `.env.example` to `.env` (never commit `.env`). The backend loads
`.env` automatically on startup; real environment variables win over it:

The four a tester must set (the rest have defaults — see `.env.example`):

| Variable | Purpose |
|---|---|
| `QUIETLOG_MODEL_API_KEY` | Meta Model API key (backend `.env`) |
| `QUIETLOG_SPARK_MODEL` | Muse Spark model identifier (backend `.env`) |
| `QUIETLOG_VOICE_MODEL` | Muse Voice Transcribe identifier (backend `.env`) |
| `EXPO_PUBLIC_API_URL` | API base URL the app talks to (frontend `.env`) |

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
  neither Mac nor Windows needs ffmpeg.

## The quote verifier

Every non-null extracted field carries an `evidence_quote` copied from the
transcript. Deterministic code (`src/quietlog/verifier.py`) requires each
quote to appear in the transcript, stated values and numbers to match their
quote, and symptom true/false values to agree with negation wording ("no
nausea" cannot support `nausea: true`). Failing fields reset to unknown and
are flagged "not supported by what you said". One targeted repair attempt
per extraction, then re-verify.

## How the summary is computed

`src/quietlog/summary.py:compute_summary` takes stored entries and a window
(30/60/90 days, chosen by the user) and returns every number on the printed
page.

**Why "not logged" is not "no attack".** An attack diary only knows what was
recorded. Treating silent days as attack-free would undercount attacks and
mislead the appointment conversation, so unlogged days are reported as
unknown.

<details>
<summary>What the summary counts</summary>

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

</details>

## Evaluation

```bash
# Same commands on Mac and Windows (venv activated; forward slashes work in PowerShell too)
python scripts/make_synthetic_diary.py   # regenerate fixtures (fixed seeds)
python scripts/evaluate.py               # writes reports/evaluation.md
python scripts/live_agent_eval.py --stub # assistant eval harness self-test (no network)
python scripts/live_agent_eval.py        # bounded live-model multi-turn eval (isolated DBs, never production)
```

M2 results (`muse-spark-1.3`, 66 transcripts, full table in
`reports/evaluation.md`): micro accuracy 92.0% (971/1056 field slots);
hallucination 0/307 before and 0/306 after the verifier; verifier rejection
12/12; summary matches planted aggregates on the fixtures plus seeds 1, 7,
99. Per-field breakdowns live in the evaluation report.

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

## License

MIT — see [LICENSE](LICENSE).
