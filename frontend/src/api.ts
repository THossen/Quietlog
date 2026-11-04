/**
 * Typed client for the Quietlog backend. The API base comes from
 * EXPO_PUBLIC_API_URL (your PC's LAN IP when running on a real device);
 * it defaults to loopback for simulators.
 */

import { File, UploadType } from 'expo-file-system';

export type EntryStatus = 'in_progress' | 'needs_processing' | 'unconfirmed' | 'confirmed';
export type EntryType = 'attack' | 'no_attack';

export interface SymptomSet {
  aura: boolean | null;
  nausea: boolean | null;
  vomiting: boolean | null;
  light_sensitivity: boolean | null;
  sound_sensitivity: boolean | null;
  smell_sensitivity: boolean | null;
  dizziness: boolean | null;
  other: string[];
}

export interface AcuteMedication {
  name: string;
  dose: string | null;
  time_taken: string | null;
  outcome: string | null;
  second_dose: boolean | null;
}

export interface AttackDetails {
  time_expression: string | null;
  attack_start: string | null;
  attack_start_precision: string;
  attack_end: string | null;
  duration_minutes: number | null;
  ongoing: boolean | null;
  severity_function: number | null;
  pain_0_10: number | null;
  symptoms: SymptomSet;
  acute_medications: AcuteMedication[];
  reported_triggers: string[];
  menstrual_context: string | null;
  notes: string | null;
  evidence: Record<string, string>;
  unsupported_fields: string[];
}

export interface Coords {
  latitude: number;
  longitude: number;
}

export interface WeatherSnapshot {
  observed_at: string | null;
  temperature_c: number | null;
  humidity_pct: number | null;
  pressure_hpa: number | null;
  pressure_tendency: 'rising' | 'falling' | 'steady' | null;
  condition: string | null;
  basis: 'log_time' | 'attack_time' | null;
  source: string | null;
}

export type RecordingStatus = 'pending' | 'transcribing' | 'transcribed' | 'failed';
export type RecordingFailureKind = 'transient' | 'permanent' | null;

/** One voice recording's transcription state. Single-entry responses carry
 * these; list responses leave the array empty. */
export interface RecordingState {
  id: string;
  status: RecordingStatus;
  failure_kind: RecordingFailureKind;
  attempts: number;
  next_attempt_at: string | null;
  error: string | null;
  transcript: string | null;
  appended: boolean;
}

export interface Entry {
  id: string;
  entry_type: EntryType;
  created_at: string;
  entry_date: string;
  audio_path: string | null;
  transcript: string | null;
  status: EntryStatus;
  attack: AttackDetails | null;
  processing_error: string | null;
  weather: WeatherSnapshot | null;
  recordings: RecordingState[];
  /** True when the entry has untranscribed (or auto-retryable) recordings. */
  pending_voice: boolean;
}

/** Stable client-generated id for one recording. Retries of the same upload
 * reuse it so the server returns the original entry instead of duplicating. */
export function newRecordingId(): string {
  const bytes = new Uint8Array(16);
  for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export interface AttackPatch {
  time_expression?: string | null;
  attack_start?: string | null;
  duration_minutes?: number | null;
  ongoing?: boolean | null;
  severity_function?: number | null;
  pain_0_10?: number | null;
  symptoms?: Partial<Record<keyof SymptomSet, boolean | string[] | null>>;
  acute_medications?: AcuteMedication[];
  reported_triggers?: string[];
  menstrual_context?: string | null;
  notes?: string | null;
}

export interface CalendarDay {
  day: string;
  state: 'attack' | 'no_attack' | 'not_logged';
  unconfirmed: boolean;
}

export interface DurationStats {
  n: number;
  median_minutes: number | null;
  min_minutes: number | null;
  max_minutes: number | null;
}

export interface MedicationStats {
  display_name: string;
  attack_entries: number;
  distinct_days: number;
  outcomes: Record<string, number>;
}

export interface Summary {
  window_start: string;
  window_end: string;
  generated_at: string;
  attack_days: number;
  no_attack_days: number;
  not_logged_days: number;
  attack_entries: number;
  unconfirmed_entries: number;
  calendar: CalendarDay[];
  duration: DurationStats;
  severity_counts: Record<string, number>;
  severity_n: number;
  symptoms: Record<string, number>;
  symptoms_other: Record<string, number>;
  symptoms_n: number;
  medications: MedicationStats[];
  triggers: Record<string, number>;
  triggers_n: number;
  pain: { mean: number | null; n: number };
  weather: { n: number; tendency_n: number; falling_n: number };
}

/** A turn whose outcome is unknown (may already have saved): never auto-rerun. */
export class TurnExpiredError extends Error {}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

const base: string = process.env.EXPO_PUBLIC_API_URL ?? 'http://127.0.0.1:8000';

/** The configured server address, shown on Home so connection issues are visible. */
export function getApiBase(): string {
  return base;
}

export async function getHealth(): Promise<boolean> {
  try {
    const body = await req<{ status: string }>('/health');
    return body.status === 'ok';
  } catch {
    return false;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method ?? 'GET';
  try {
    return await attempt<T>(path, init);
  } catch (err) {
    // Reads are side-effect-free, so a transient first-attempt failure
    // (cold radio, dropped keep-alive) is worth one silent retry. Writes
    // never retry here: repeating a POST could save twice.
    const retryable =
      method === 'GET' &&
      err instanceof ApiError &&
      (err.status === 0 || err.status === 502 || err.status === 503 || err.status === 504);
    if (!retryable) throw err;
    await new Promise((resolve) => setTimeout(resolve, 400));
    return attempt<T>(path, init);
  }
}

async function attempt<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(base + path, init);
  } catch {
    throw new ApiError(
      0,
      'Cannot reach Quietlog. Is the server running, and is EXPO_PUBLIC_API_URL set to this computer?',
    );
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
    } catch {
      /* keep the default */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/**
 * POST a voice note with the native file uploader. fetch() + FormData file
 * parts fail to read the recording on some devices (surfacing as a network
 * error even when the server is reachable); File.upload() streams the file
 * natively instead. Same endpoint, fields, and errors as req().
 */
async function uploadAudio<T>(
  path: string,
  audioUri: string,
  params: Record<string, string>,
): Promise<T> {
  let status: number;
  let text: string;
  try {
    const result = await new File(audioUri).upload(base + path, {
      uploadType: UploadType.MULTIPART,
      fieldName: 'audio',
      mimeType: 'audio/wav',
      parameters: params,
    });
    status = result.status;
    text = result.body;
  } catch {
    throw new ApiError(
      0,
      'Cannot reach Quietlog. Is the server running, and is EXPO_PUBLIC_API_URL set to this computer?',
    );
  }
  if (status < 200 || status >= 300) {
    let detail = `Request failed (${status})`;
    try {
      const parsed = JSON.parse(text) as { detail?: unknown };
      if (typeof parsed.detail === 'string') detail = parsed.detail;
    } catch {
      /* keep the default */
    }
    throw new ApiError(status, detail);
  }
  return JSON.parse(text) as T;
}

function appendCoords(form: FormData, coords?: Coords | null): void {
  if (!coords) return;
  form.append('latitude', String(coords.latitude));
  form.append('longitude', String(coords.longitude));
}

export async function saveAttack(args: {
  audioUri?: string;
  transcript?: string;
  coords?: Coords | null;
  recordingId?: string;
}): Promise<Entry> {
  if (args.audioUri) {
    const body = await uploadAudio<{ saved: boolean; entry: Entry }>('/entries', args.audioUri, {
      entry_type: 'attack',
      created_at: new Date().toISOString(),
      ...(args.recordingId ? { recording_id: args.recordingId } : {}),
      ...(args.transcript?.trim() ? { transcript: args.transcript.trim() } : {}),
      ...(args.coords
        ? { latitude: String(args.coords.latitude), longitude: String(args.coords.longitude) }
        : {}),
    });
    return body.entry;
  }
  const form = new FormData();
  form.append('entry_type', 'attack');
  form.append('created_at', new Date().toISOString());
  if (args.transcript?.trim()) form.append('transcript', args.transcript.trim());
  appendCoords(form, args.coords);
  const body = await req<{ saved: boolean; entry: Entry }>('/entries', {
    method: 'POST',
    body: form,
  });
  return body.entry;
}

export async function saveCheckin(coords?: Coords | null): Promise<Entry> {
  const form = new FormData();
  form.append('entry_type', 'no_attack');
  form.append('created_at', new Date().toISOString());
  appendCoords(form, coords);
  const body = await req<{ saved: boolean; entry: Entry }>('/entries', {
    method: 'POST',
    body: form,
  });
  return body.entry;
}

export async function processEntry(
  id: string,
  coords?: Coords | null,
  manual: boolean = true,
): Promise<Entry> {
  const form = new FormData();
  form.append('manual', manual ? 'true' : 'false');
  appendCoords(form, coords);
  return req<Entry>(`/entries/${id}/process`, { method: 'POST', body: form });
}

/** Transcribe an entry's pending recordings without extracting. Safe on
 * running timers; returns the entry with per-recording state. */
export async function transcribeEntry(id: string, manual: boolean = true): Promise<Entry> {
  const form = new FormData();
  form.append('manual', manual ? 'true' : 'false');
  return req<Entry>(`/entries/${id}/transcribe`, { method: 'POST', body: form });
}


/** Fold follow-up answers into an understood note, returning it for reprocessing. */
export async function answerFollowups(
  id: string,
  answers: Array<{ question: string; answer: string }>,
): Promise<Entry> {
  return req(`/entries/${id}/answer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answers }),
  });
}

export interface AskResponse {
  answer: string;
  entries_consulted: number;
  window_days: number;
  cached: boolean;
}

/** Ask a question answered strictly from the diary window. */
export async function askDiary(
  question: string,
  windowDays: 30 | 60 | 90 = 90,
): Promise<AskResponse> {
  return req('/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, window_days: windowDays }),
  });
}

export interface NarrativeResponse {
  narrative: string;
  window_days: number;
  cached: boolean;
}

/** Draft a doctor-summary paragraph from the deterministic summary. */
export async function draftNarrative(windowDays: 30 | 60 | 90): Promise<NarrativeResponse> {
  return req('/summary/narrative', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ window_days: windowDays }),
  });
}

export interface AgentFollowup {
  id: string;
  question: string;
  kind: string;
  options?: string[];
}

export interface EntryReceipt {
  entry_id: string;
  date: string | null;
  status: EntryStatus;
  transcript?: string;
  processing_error?: boolean;
  pain?: number;
  medications?: string[];
  start?: string;
  end?: string | null;
  duration_minutes?: number | null;
}

export function receiptFromEntry(entry: Entry): EntryReceipt {
  return {
    entry_id: entry.id, date: entry.entry_date, status: entry.status,
    transcript: entry.transcript ?? '', processing_error: !!entry.processing_error,
    pain: entry.attack?.pain_0_10 ?? undefined,
    medications: entry.attack?.acute_medications.map(m => m.name),
    start: entry.attack?.attack_start ?? undefined,
    end: entry.attack?.attack_end, duration_minutes: entry.attack?.duration_minutes,
  };
}

export interface AgentAction {
  tool: string;
  ok: boolean;
  label: string;
  entry_id: string | null;
  followups?: AgentFollowup[];
  data?: Record<string, unknown> & { receipt?: EntryReceipt };
  status?: string;
}

export interface AgentTurnState {
  timer_running: boolean;
  discussed_entry_id?: string;
  active_attack_id?: string;
  pending_entry_id?: string;
}

export interface AgentTurn {
  request_id: string;
  status: 'pending' | 'completed' | 'failed' | 'expired';
  response?: AgentResponse;
}

export interface AgentResponse {
  reply: string;
  actions: AgentAction[];
  state?: AgentTurnState;
  status?: string;
  replayed?: boolean;
  cached: boolean;
}

/** Transcribe one voice note without saving an entry. */
export async function transcribeAudio(audioUri: string): Promise<string> {
  const body = await uploadAudio<{ transcript: string }>('/transcribe', audioUri, {});
  return body.transcript;
}

/** One chat turn with the diary assistant. */
export async function agentChat(
  message: string,
  history: Array<{ role: 'user' | 'assistant'; content: string }>,
  coords?: Coords | null,
  opts?: { requestId?: string; context?: AgentTurnState | null },
): Promise<AgentResponse> {
  return req('/agent', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      history: history.slice(-10),
      ...(opts?.requestId ? { request_id: opts.requestId } : {}),
      ...(opts?.context ? { context: opts.context } : {}),
      ...(coords ? { latitude: coords.latitude, longitude: coords.longitude } : {}),
    }),
  });
}

/** Poll a claimed turn after a 409: pending, completed (with response), failed, expired. */
export async function getAgentTurn(requestId: string): Promise<AgentTurn> {
  return req(`/agent/turns/${encodeURIComponent(requestId)}`);
}

/** Start the one-tap attack timer. Repeat taps return the running timer. */
export async function startAttack(coords?: Coords | null): Promise<Entry> {
  const form = new FormData();
  form.append('created_at', new Date().toISOString());
  appendCoords(form, coords);
  const body = await req<{ saved: boolean; entry: Entry }>('/entries/start', {
    method: 'POST',
    body: form,
  });
  return body.entry;
}

/** Stop the timer: stamps the end, measures duration, and processes inline so
 * no recording waits for a manual first attempt. */
export async function endAttack(id: string, coords?: Coords | null): Promise<Entry> {
  const form = new FormData();
  form.append('ended_at', new Date().toISOString());
  appendCoords(form, coords);
  return req(`/entries/${id}/end`, { method: 'POST', body: form });
}

/** Attach a voice note or typed text to a running or ended timer. Audio is
 * transcribed inline without stopping a running timer; retries reuse
 * recordingId so nothing is stored or appended twice. */
export async function attachNote(
  id: string,
  args: { audioUri?: string; transcript?: string; recordingId?: string },
): Promise<Entry> {
  if (args.audioUri) {
    return uploadAudio(`/entries/${id}/note`, args.audioUri, {
      ...(args.recordingId ? { recording_id: args.recordingId } : {}),
      ...(args.transcript?.trim() ? { transcript: args.transcript.trim() } : {}),
    });
  }
  const form = new FormData();
  if (args.transcript?.trim()) form.append('transcript', args.transcript.trim());
  return req(`/entries/${id}/note`, { method: 'POST', body: form });
}

/** Delete any entry. Cannot be undone; callers confirm first. */
export async function deleteEntry(id: string): Promise<void> {
  await req<{ deleted: boolean }>(`/entries/${id}`, { method: 'DELETE' });
}

export async function listEntries(params?: {
  status?: EntryStatus;
  entry_type?: EntryType;
  start?: string;
  end?: string;
  limit?: number;
}): Promise<{ entries: Entry[]; count: number }> {
  const q = new URLSearchParams();
  if (params?.status) q.set('status', params.status);
  if (params?.entry_type) q.set('entry_type', params.entry_type);
  if (params?.start) q.set('start', params.start);
  if (params?.end) q.set('end', params.end);
  if (params?.limit) q.set('limit', String(params.limit));
  const suffix = q.size > 0 ? `?${q.toString()}` : '';
  return req(`/entries${suffix}`);
}

export async function getEntry(id: string): Promise<Entry> {
  return req(`/entries/${id}`);
}

export async function getSummary(windowDays: 30 | 60 | 90): Promise<Summary> {
  return req(`/summary?window_days=${windowDays}`);
}

export async function patchEntry(
  id: string,
  patch: { attack?: AttackPatch; confirm?: boolean },
): Promise<Entry> {
  return req(`/entries/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

/** Short honest one-liner for list rows. Never invents details. */
export function summarize(entry: Entry): string {
  if (entry.entry_type === 'no_attack') return 'No attack logged';
  if (entry.status === 'in_progress') return 'Attack in progress';
  if (entry.status === 'needs_processing' && !entry.transcript && !entry.audio_path) {
    return 'Ended timer · no note yet';
  }
  const attack = entry.attack;
  if (!attack) return 'Voice note saved';
  const bits: string[] = [];
  const sev = attack.severity_function;
  if (sev === 1) bits.push('Mild');
  else if (sev === 2) bits.push('Moderate');
  else if (sev === 3) bits.push('Severe');
  if (attack.time_expression) bits.push(`since ${attack.time_expression}`);
  const meds = attack.acute_medications.length;
  if (meds > 0) bits.push(`${meds} med${meds === 1 ? '' : 's'}`);
  if (bits.length === 0) {
    const words = (entry.transcript ?? '').split(/\s+/).slice(0, 8).join(' ');
    return words ? `${words}…` : 'Attack note saved';
  }
  return bits.join(' · ');
}

/** One-line weather readout, or null when nothing was recorded. */
export function formatWeather(weather: WeatherSnapshot | null): string | null {
  if (!weather) return null;
  const bits: string[] = [];
  if (weather.pressure_hpa !== null && weather.pressure_hpa !== undefined) {
    const tendency =
      weather.pressure_tendency === 'falling'
        ? ', falling'
        : weather.pressure_tendency === 'rising'
          ? ', rising'
          : '';
    bits.push(`${Math.round(weather.pressure_hpa)} hPa${tendency}`);
  }
  if (weather.condition) bits.push(weather.condition);
  if (weather.temperature_c !== null && weather.temperature_c !== undefined) {
    bits.push(`${Math.round(weather.temperature_c)}°C`);
  }
  return bits.length > 0 ? bits.join(' · ') : null;
}

export const SYMPTOM_LABELS: Record<keyof Omit<SymptomSet, 'other'>, string> = {
  aura: 'Aura',
  nausea: 'Nausea',
  vomiting: 'Vomiting',
  light_sensitivity: 'Light sensitivity',
  sound_sensitivity: 'Sound sensitivity',
  smell_sensitivity: 'Smell sensitivity',
  dizziness: 'Dizziness',
};

export function formatDay(isoDate: string): string {
  const [y, m, d] = isoDate.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  });
}
