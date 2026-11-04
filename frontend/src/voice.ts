/**
 * Voice processing driver: automatic transcription + extraction with bounded
 * recovery. Lives outside any screen so background recovery (focus sweeps,
 * foreground resumes) never depends on Capture staying mounted.
 *
 * Two flows share it:
 * - recoverEntry: foreground flow with phase callbacks (Capture). Runs
 *   transcribe, shows the transcript, then extracts — retrying transient
 *   failures with backoff before surfacing a manual retry.
 * - sweepPendingVoice: background flow (Home focus/foreground). Best-effort,
 *   overlap-safe, never throws.
 */

import {
  ApiError,
  listEntries,
  processEntry,
  transcribeAudio,
  transcribeEntry,
  type Coords,
  type Entry,
} from './api';

export type VoicePhase =
  | 'transcribing'
  | 'transcript-ready'
  | 'organizing'
  | 'done'
  | 'failed';

export interface VoiceProgress {
  phase: VoicePhase;
  entry: Entry;
  /** User-facing failure line; set only when phase is failed. */
  detail: string | null;
  /** True when retrying cannot help (invalid audio, auth/setup problem). */
  permanent: boolean;
}

/** Total attempts per foreground recovery run, and the waits between them. */
const AUTO_ATTEMPTS = 3;
const BACKOFF_MS = [1200, 2500];
const SWEEP_LIMIT = 10;

/** Entries with a pass already running (foreground or sweep): skip, don't stack. */
const inflight = new Set<string>();
let sweeping = false;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Permanent failures (HTTP 422) never auto-retry: the audio or setup needs attention. */
export function isPermanentFailure(err: unknown): boolean {
  return err instanceof ApiError && err.status === 422;
}

function isRetryable(err: unknown): boolean {
  return (
    err instanceof ApiError &&
    (err.status === 0 ||
      err.status === 502 ||
      err.status === 503 ||
      err.status === 504)
  );
}

function failureDetail(entry: Entry): { detail: string; permanent: boolean } {
  const failed = entry.recordings.filter((r) => r.status === 'failed');
  const permanent = failed.find((r) => r.failure_kind === 'permanent');
  if (permanent?.error) return { detail: permanent.error, permanent: true };
  if (entry.processing_error) return { detail: entry.processing_error, permanent: false };
  const first = failed[0]?.error;
  if (first) return { detail: first, permanent: false };
  return { detail: 'Could not finish processing this note.', permanent: false };
}

function workRemains(entry: Entry): boolean {
  return entry.recordings.some(
    (r) =>
      r.status === 'pending' ||
      r.status === 'transcribing' ||
      (r.status === 'failed' && r.failure_kind === 'transient'),
  );
}

/**
 * Drive one entry from saved to understood, with bounded automatic recovery.
 * Progress callback receives every phase (transcript included) so the UI can
 * narrate honestly. Resolves with the final entry; the failed phase (not a
 * throw) reports give-up, with detail + permanent set. Transport failures
 * beyond the attempt budget rethrow for the caller to surface.
 */
export async function recoverEntry(
  entryId: string,
  coords: Coords | null | undefined,
  onProgress: (progress: VoiceProgress) => void,
  opts?: { attempts?: number; cancelled?: () => boolean },
): Promise<Entry> {
  const attempts = opts?.attempts ?? AUTO_ATTEMPTS;
  const isCancelled = opts?.cancelled ?? (() => false);
  if (inflight.has(entryId)) {
    // A sweep or another screen already drives this entry: don't stack a
    // second pass; the caller can re-read the entry instead.
    throw new Error('Processing is already running for this note.');
  }
  inflight.add(entryId);
  try {
    let last: Entry | null = null;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      if (isCancelled()) break;
      if (attempt > 0) {
        await sleep(BACKOFF_MS[Math.min(attempt - 1, BACKOFF_MS.length - 1)]);
        if (isCancelled()) break;
      }
      let entry: Entry;
      try {
        entry = await transcribeEntry(entryId, true);
      } catch (err) {
        if (isPermanentFailure(err) || !isRetryable(err) || attempt + 1 >= attempts) {
          throw err;
        }
        continue;
      }
      last = entry;
      onProgress({
        phase: 'transcript-ready',
        entry,
        detail: null,
        permanent: false,
      });
      if (workRemains(entry)) {
        if (attempt + 1 >= attempts) {
          const { detail, permanent } = failureDetail(entry);
          onProgress({ phase: 'failed', entry, detail, permanent });
          return entry;
        }
        continue;
      }
      if (!entry.transcript?.trim()) {
        const { detail, permanent } = failureDetail(entry);
        onProgress({ phase: 'failed', entry, detail, permanent });
        return entry;
      }
      if (entry.status === 'in_progress') {
        // Running timer: transcription is the whole job; extraction waits.
        onProgress({ phase: 'done', entry, detail: null, permanent: false });
        return entry;
      }
      onProgress({ phase: 'organizing', entry, detail: null, permanent: false });
      let processed: Entry;
      try {
        processed = await processEntry(entryId, coords ?? null, true);
      } catch (err) {
        if (isPermanentFailure(err) || !isRetryable(err) || attempt + 1 >= attempts) {
          throw err;
        }
        continue;
      }
      last = processed;
      if (processed.status === 'unconfirmed' || processed.status === 'confirmed') {
        onProgress({ phase: 'done', entry: processed, detail: null, permanent: false });
        return processed;
      }
      if (attempt + 1 >= attempts) {
        const { detail, permanent } = failureDetail(processed);
        onProgress({ phase: 'failed', entry: processed, detail, permanent });
        return processed;
      }
      // Extraction failed but attempts remain: loop re-runs transcribe
      // (a no-op once transcripts are persisted) and tries extraction again.
    }
    if (last === null) throw new Error('Processing was cancelled.');
    return last;
  } finally {
    inflight.delete(entryId);
  }
}

/**
 * Transcribe one chat voice note with bounded retries. Resolves with the
 * transcript (possibly empty — the caller decides what empty means) and
 * throws the last ApiError: 422 (permanent, no retry was attempted) or a
 * retryable status after the budget ran out. The caller keeps the audio URI
 * so failures can offer "Retry transcription" on the SAME recording.
 */
export async function transcribeWithRetry(
  audioUri: string,
  attempts: number = AUTO_ATTEMPTS,
): Promise<string> {
  let lastErr: unknown = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (attempt > 0) {
      await sleep(BACKOFF_MS[Math.min(attempt - 1, BACKOFF_MS.length - 1)]);
    }
    try {
      return await transcribeAudio(audioUri);
    } catch (err) {
      lastErr = err;
      if (isPermanentFailure(err) || !isRetryable(err)) throw err;
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error('Could not transcribe.');
}

/**
 * Background sweep: pick up entries with audio that still need work
 * (interrupted Capture flows, restarted backend, exhausted-then-untouched
 * notes) and run one quiet pass over each. Overlap-safe and silent: never
 * throws, never touches entries another pass is driving.
 */
export async function sweepPendingVoice(coords?: Coords | null): Promise<void> {
  if (sweeping) return;
  sweeping = true;
  try {
    const [pending, running, unconfirmed] = await Promise.all([
      listEntries({ status: 'needs_processing', limit: 100 }),
      listEntries({ status: 'in_progress', entry_type: 'attack', limit: 5 }),
      listEntries({ status: 'unconfirmed', limit: 100 }),
    ]);
    const candidates = [...running.entries, ...pending.entries, ...unconfirmed.entries]
      .filter(
        (entry) =>
          !inflight.has(entry.id) &&
          // Voice work waiting in any status (an unconfirmed entry whose
          // spoken note never transcribed must still heal)...
          (entry.pending_voice ||
            // ...plus ended notes whose transcript landed but extraction
            // never finished: retry it quietly, once per visit.
            (entry.status === 'needs_processing' && Boolean(entry.audio_path))),
      )
      .slice(0, SWEEP_LIMIT);
    for (const entry of candidates) {
      if (inflight.has(entry.id)) continue;
      inflight.add(entry.id);
      try {
        // One call covers both states: running timers get a transcribe-only
        // pass, ended entries get the full pass. manual=false respects
        // backoffs, budgets, and permanent failures.
        await processEntry(entry.id, coords ?? null, false);
      } catch {
        /* next sweep retries; failures stay recorded server-side */
      } finally {
        inflight.delete(entry.id);
      }
    }
  } catch {
    /* offline or list failed: stay quiet, try on the next trigger */
  } finally {
    sweeping = false;
  }
}
