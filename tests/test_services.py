"""Service unit tests: cache, config guards, parsing, repair merge.

All network is stubbed. Nothing here needs an API key or the internet.
"""

import io
import json
import wave
from datetime import datetime, timezone

import pytest

from quietlog import cache as cache_mod
from quietlog import llm_service, transcribe_service


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ("" if payload is None else json.dumps(payload))

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


def _chat_payload(content):
    return {"choices": [{"message": {"content": content}}]}


# --- cache -----------------------------------------------------------------


def test_cache_roundtrip_and_miss(tmp_path):
    key = cache_mod.request_hash({"a": 1})
    assert cache_mod.read_cached(tmp_path, key) is None
    cache_mod.write_cached(tmp_path, key, {"ok": True})
    assert cache_mod.read_cached(tmp_path, key) == {"ok": True}
    assert cache_mod.request_hash({"a": 1}) == key  # stable


def test_cache_ignores_corrupt_and_versioned_entries(tmp_path):
    key = cache_mod.request_hash({"a": 1})
    path = cache_mod.cache_path(tmp_path, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert cache_mod.read_cached(tmp_path, key) is None
    path.write_text(json.dumps({"cache_version": 999, "request_hash": key,
                                "response": {}}), encoding="utf-8")
    assert cache_mod.read_cached(tmp_path, key) is None


# --- llm config guards ------------------------------------------------------


def test_llm_config_defaults_and_env(monkeypatch, tmp_path):
    monkeypatch.delenv("QUIETLOG_SPARK_MODEL", raising=False)
    monkeypatch.delenv("QUIETLOG_MODEL_TIER", raising=False)
    cfg = llm_service.load_config(cache_dir=str(tmp_path))
    assert cfg.model == "muse-spark-1.3"
    assert cfg.base_url == "https://api.meta.ai/v1"


def test_contributor_model_refused(monkeypatch, tmp_path):
    with pytest.raises(llm_service.ModelConfigError, match="contributor"):
        llm_service.load_config(model="muse-spark-1.3-contributor",
                                cache_dir=str(tmp_path))


def test_nonstandard_tier_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("QUIETLOG_MODEL_TIER", "contributor")
    with pytest.raises(llm_service.ModelConfigError, match="standard"):
        llm_service.load_config(cache_dir=str(tmp_path))


# --- llm parsing / fallback / cache -----------------------------------------


def test_parse_model_json_plain_fenced_and_bad():
    assert llm_service.parse_model_json('{"a": 1}') == {"a": 1}
    assert llm_service.parse_model_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm_service.parse_model_json('prefix {"a": 1} suffix') == {"a": 1}
    with pytest.raises(llm_service.LLMError):
        llm_service.parse_model_json("no json here")
    with pytest.raises(llm_service.LLMError):
        llm_service.parse_model_json("[1, 2]")


def test_extract_raw_uses_cache_and_parses(tmp_path):
    calls = []

    def stub(url, *, headers, payload, timeout):
        calls.append(payload)
        return FakeResponse(200, _chat_payload('{"time_expression": null}'))

    cfg = llm_service.load_config(api_key="test-key", cache_dir=str(tmp_path))
    payload, cached = llm_service.extract_raw("hello", config=cfg, http_post=stub)
    assert payload == {"time_expression": None}
    assert cached is False
    assert calls[0]["response_format"]["type"] == "json_schema"
    payload2, cached2 = llm_service.extract_raw("hello", config=cfg, http_post=stub)
    assert payload2 == {"time_expression": None}
    assert cached2 is True
    assert len(calls) == 1  # cache hit: no second call


def test_response_format_falls_back_on_400(tmp_path):
    seen = []

    def stub(url, *, headers, payload, timeout):
        seen.append((payload.get("response_format") or {}).get("type"))
        if payload.get("response_format", {}).get("type") == "json_schema":
            return FakeResponse(400, None, "unknown response_format")
        return FakeResponse(200, _chat_payload("{}"))

    cfg = llm_service.load_config(api_key="k", cache_dir=str(tmp_path))
    payload, _ = llm_service.extract_raw("hi", config=cfg, http_post=stub)
    assert payload == {}
    assert seen[0] == "json_schema"
    assert seen[1] == "json_object"


def test_missing_key_without_cache_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("QUIETLOG_MODEL_API_KEY", raising=False)
    cfg = llm_service.load_config(api_key=None, cache_dir=str(tmp_path))
    assert cfg.api_key is None
    with pytest.raises(llm_service.MissingApiKeyError):
        llm_service.extract_raw("hi", config=cfg, http_post=lambda *a, **k: None)


def test_merge_repairs_sets_by_path():
    raw = {
        "severity_function": {"value": 3, "quote": "zzz"},
        "symptoms": {"nausea": {"value": True, "quote": "zzz"}},
        "acute_medications": [{"name": {"value": "x", "quote": "x"},
                                "dose": {"value": "big", "quote": "zzz"}}],
        "reported_triggers": [{"value": "y", "quote": "zzz"}],
    }
    merged = llm_service.merge_repairs(raw, [
        {"path": "severity_function", "field": None},
        {"path": "symptoms.nausea",
         "field": {"value": False, "quote": "No nausea"}},
        {"path": "acute_medications[0].dose",
         "field": {"value": "200mg", "quote": "200mg"}},
        {"path": "reported_triggers[0]", "field": None},
        {"path": "bogus[9].nope", "field": None},  # ignored
    ])
    assert merged["severity_function"] is None
    assert merged["symptoms"]["nausea"]["value"] is False
    assert merged["acute_medications"][0]["dose"]["value"] == "200mg"
    assert merged["reported_triggers"][0] is None
    assert raw["severity_function"] is not None  # input untouched


def test_extract_attack_repairs_then_reverifies(tmp_path):
    transcript = "Started at 3pm, mild. No nausea."
    first = {
        "time_expression": {"value": "at 3pm", "quote": "Started at 3pm"},
        "severity_function": {"value": 3, "quote": "fabricated words"},
        "symptoms": {"nausea": {"value": True, "quote": "No nausea"}},
    }
    script = [
        FakeResponse(200, _chat_payload(json.dumps(first))),
        FakeResponse(200, _chat_payload(json.dumps({"repairs": [
            {"path": "severity_function", "field": None},
            {"path": "symptoms.nausea",
             "field": {"value": False, "quote": "No nausea"}},
        ]}))),
    ]

    def stub(url, *, headers, payload, timeout):
        return script.pop(0)

    cfg = llm_service.load_config(api_key="k", cache_dir=str(tmp_path))
    result = llm_service.extract_attack(transcript, config=cfg, http_post=stub)
    assert result.repaired is True
    assert len(result.failures_before) == 2
    assert result.attack.severity_function is None
    assert result.attack.symptoms.nausea is False
    assert result.verified.failures == []


def test_extract_attack_fills_duration_from_time_range(tmp_path):
    transcript = "Headache from 8am to 12pm, then gone."
    first = {
        "time_expression": {"value": "from 8am to 12pm", "quote": "from 8am to 12pm"},
        "duration_minutes": None,
    }
    script = [FakeResponse(200, _chat_payload(json.dumps(first)))]

    def stub(url, *, headers, payload, timeout):
        return script.pop(0)

    cfg = llm_service.load_config(api_key="k", cache_dir=str(tmp_path))
    ref = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)
    result = llm_service.extract_attack(transcript, reference_time=ref,
                                        config=cfg, http_post=stub)
    assert result.attack.duration_minutes == 240
    assert result.attack.attack_start is not None
    assert (result.attack.attack_start.hour, result.attack.attack_start.minute) == (8, 0)


# --- transcribe service -----------------------------------------------------


def _wav_bytes(*, channels=1, width=2, rate=24000, seconds=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00" * rate * channels * width * seconds)
    return buf.getvalue()


def test_transcribe_rejects_non_wav(tmp_path):
    audio = tmp_path / "note.mp3"
    audio.write_bytes(b"ID3 not a wav")
    cfg = transcribe_service.load_config(api_key="k", cache_dir=str(tmp_path))
    with pytest.raises(transcribe_service.TranscribeError, match="WAV"):
        transcribe_service.transcribe_file(audio, config=cfg)


def test_transcribe_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe_service, "MAX_BYTES", 10)
    audio = tmp_path / "note.wav"
    audio.write_bytes(_wav_bytes())
    cfg = transcribe_service.load_config(api_key="k", cache_dir=str(tmp_path))
    with pytest.raises(transcribe_service.TranscribeError, match="32 MB"):
        transcribe_service.transcribe_file(audio, config=cfg)


def test_transcribe_bad_mode_rejected(tmp_path):
    with pytest.raises(transcribe_service.TranscribeError, match="mode"):
        transcribe_service.load_config(mode="karaoke", cache_dir=str(tmp_path))


def test_transcribe_posts_and_caches(tmp_path):
    audio = tmp_path / "note.wav"
    audio.write_bytes(_wav_bytes())
    calls = []

    def stub(url, *, headers, request_json, audio_name, audio_bytes, timeout):
        calls.append((url, json.loads(request_json)))
        assert headers["Authorization"] == "Bearer k"
        return FakeResponse(200, {"transcript": "migraine since lunch",
                                  "audioDurationMs": 1000, "sessionId": "s1"})

    cfg = transcribe_service.load_config(api_key="k", cache_dir=str(tmp_path))
    first = transcribe_service.transcribe_file(audio, config=cfg, http_post=stub)
    assert first.transcript == "migraine since lunch"
    assert first.cached is False
    assert calls[0][0] == "https://api.meta.ai/v1/asr/transcribe"
    assert calls[0][1]["model"] == "muse-voice-transcribe-1.0"
    assert calls[0][1]["audioEncoding"] == "WAV"
    second = transcribe_service.transcribe_file(audio, config=cfg, http_post=stub)
    assert second.transcript == "migraine since lunch"
    assert second.cached is True
    assert len(calls) == 1


def test_transcribe_maps_error_status(tmp_path):
    audio = tmp_path / "note.wav"
    audio.write_bytes(_wav_bytes())
    cfg = transcribe_service.load_config(api_key="bad", cache_dir=str(tmp_path))

    def stub(url, **kwargs):
        return FakeResponse(401, None, "bad key")

    with pytest.raises(transcribe_service.TranscribeError, match="401"):
        transcribe_service.transcribe_file(audio, config=cfg, http_post=stub)
