# Quietlog

Quietlog is a voice-first migraine diary. Logging during an attack can be as simple as tapping **Log attack** and saying what happened. Quietlog saves the recording, transcribes it, and organizes the details into an entry you can review. When it is time for an appointment, export a printable summary of your diary.

Built for the Meta Global AI Developer Hackathon with **Muse Voice Transcribe** for speech to text, **Muse Spark 1.3** for the assistant and field extraction, and **Expo, FastAPI, and SQLite** for the app.

## Try the workflow

1. Record a note such as “Headache since lunch, pain seven out of ten.” Quietlog saves the recording and starts transcription automatically.
2. Open the assistant and say “Also, I felt nauseous.” When the target is clear, the assistant updates the same attack and shows which entry it changed.
3. Review the entry against your original words. Open **Reports** to export a 30-, 60-, or 90-day summary as a PDF.

You can also start an attack timer, add details while it runs, and stop it when the attack ends. Start, stop, and no-headache check-ins have direct buttons. History and Insights distinguish days with no recorded entry from explicit no-headache check-ins.

## Run locally

You’ll need Python 3.11+, Node.js with npm, a Meta Model API key, and [Expo Go](https://expo.dev/go) for testing on a phone.

**Backend.** From the repository root, create a virtual environment and copy the backend configuration.

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
Copy-Item .env.example .env
```

macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
cp .env.example .env
```

Set `QUIETLOG_MODEL_API_KEY` in the root `.env`, then install and start the API:

```sh
python -m pip install -e ".[dev]"
python -m uvicorn quietlog.main:app --host 0.0.0.0 --port 8000
```

For simulator-only testing, bind to `127.0.0.1`. Check [API health](http://127.0.0.1:8000/health) on the computer before opening the app.

**Mobile app.** In a second terminal, enter `frontend` and run `npm ci`. Copy that folder’s `.env.example` to `.env` and set `EXPO_PUBLIC_API_URL`:

| Test device | API URL |
|---|---|
| Phone on the same Wi-Fi | `http://<computer-LAN-IP>:8000` |
| iOS Simulator | `http://127.0.0.1:8000` |
| Android Emulator | `http://10.0.2.2:8000` |

On Windows, run `ipconfig` to find the computer’s Wi-Fi IPv4 address. Start Expo with `npx expo start -c` and scan the QR code on your phone. For a simulator or emulator, press `i` or `a` in the Expo terminal.

For a synthetic demo diary, set `QUIETLOG_DB=data/demo.db` in the backend `.env`, run `python scripts/seed_demo.py` from the repository root, then start or restart the backend with that configuration.

## How it works

- **Voice recovery.** Recordings and transcripts are saved separately. An extraction failure keeps the transcript available for retry, and repeated uploads use the same recording ID.
- **Assistant actions.** The assistant uses validated tools to log, find, and summarize entries. It asks when the target is ambiguous. Receipts come from tool results, and request IDs prevent completed turns from writing twice.
- **Review and reports.** Extracted values are checked against quotes from the original note. User edits survive reprocessing, and reports calculate counts from saved entries. An optional AI-drafted paragraph is labeled if included in the PDF.

## Verification

From the repository root with the virtual environment active:

```sh
python -m pytest
python scripts/live_agent_eval.py --stub
npm --prefix frontend run typecheck
```

`python scripts/live_agent_eval.py` runs a bounded live-model evaluation on isolated synthetic diaries, using API credits. It checks tool choices and stored results for timer workflows, ambiguous entries, date corrections, and retries. These scenarios exercise the software with synthetic data.

## Privacy and limitations

The app stores its diary database and recordings on the machine running the backend. Voice transcription, field extraction, and assistant requests send the necessary audio or text to Meta’s Model API. The backend requires Standard mode and rejects Contributor models. The repository contains synthetic diary fixtures.

Quietlog is a single-user diary and appointment aid. It does not provide medical advice or diagnoses. Review transcriptions and extracted details before sharing a report.

[Project specification](QUIETLOG_SPEC.md) · [MIT License](LICENSE)
