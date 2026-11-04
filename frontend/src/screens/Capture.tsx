import { useEffect, useRef, useState } from 'react';
import { KeyboardAvoidingView, Platform, StyleSheet, Text, TextInput, View } from 'react-native';
import { useNavigation, useRoute, type RouteProp } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import type { Entry } from '../api';
import { answerFollowups, attachNote, getEntry, newRecordingId, saveAttack } from '../api';
import { recoverEntry, type VoiceProgress } from '../voice';
import { ensureMicPermission, ensureWav16k, formatClock, useAudioStream } from '../audio';
import { thump } from '../haptics';
import { getLogCoords } from '../location';
import { colors, fonts, shadows, sizes } from '../theme';
import ScreenShell from '../components/ScreenShell';
import { ErrorText, GhostButton, PrimaryButton, ScalePressable } from '../components/ui';
import type { RootStackParamList } from '../nav';

type Phase =
  | { name: 'ready' }
  | { name: 'recording' }
  | { name: 'typing' }
  | { name: 'working'; label: string }
  | { name: 'done'; entry: Entry; understood: boolean; detail: string | null; running: boolean };

type Attach = 'loading' | 'in_progress' | 'needs_processing' | 'settled' | 'missing' | null;

interface Followup {
  id: string;
  question: string;
  kind: 'chips' | 'text';
  options?: string[];
}

const MED_OUTCOME_OPTIONS = ['Helped', 'Helped a bit', 'No help', 'Side effects'];

/**
 * Gaps worth one follow-up round, most valuable first. Deterministic rules
 * over the extracted fields; at most two so capturing stays quick.
 */
function followupsFor(entry: Entry): Followup[] {
  const attack = entry.attack;
  if (!attack) return [];
  const gaps: Followup[] = [];
  if (attack.pain_0_10 === null && attack.severity_function === null) {
    gaps.push({ id: 'pain', question: 'How bad was the pain, 0–10?', kind: 'text' });
  }
  const medIndex = attack.acute_medications.findIndex(
    (med) => !med.outcome || med.outcome === 'unknown',
  );
  if (medIndex >= 0) {
    const name = attack.acute_medications[medIndex].name;
    gaps.push({
      id: `med:${medIndex}`,
      question: `Did the ${name} help?`,
      kind: 'chips',
      options: MED_OUTCOME_OPTIONS,
    });
  }
  if (attack.duration_minutes === null && attack.ongoing !== true) {
    gaps.push({ id: 'duration', question: 'How long did it last?', kind: 'text' });
  }
  if (attack.attack_start === null) {
    gaps.push({ id: 'start', question: 'When did it start?', kind: 'text' });
  }
  return gaps.slice(0, 2);
}

function CaptureBody() {
  const route = useRoute<RouteProp<RootStackParamList, 'Capture'>>();
  const navigation = useNavigation<NativeStackNavigationProp<RootStackParamList>>();
  const entryId = route.params?.entryId;
  const onHome = () => navigation.goBack();
  const onReviewEntry = (id: string) => navigation.replace('Detail', { id });
  const { stream } = useAudioStream({ sampleRate: 16000, channels: 1, encoding: 'int16' });
  const [phase, setPhase] = useState<Phase>({ name: 'ready' });
  const [seconds, setSeconds] = useState(0);
  const [typed, setTyped] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [attach, setAttach] = useState<Attach>(entryId ? 'loading' : null);
  const [askedRound, setAskedRound] = useState(false);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [answering, setAnswering] = useState(false);
  // Finished recording kept until its upload succeeds: a failed upload
  // retries with the same recording id instead of asking to re-record.
  const [pendingAudio, setPendingAudio] = useState<{ uri: string; recordingId: string } | null>(null);
  // Failed entry that "Type instead" appends typed text to (no new entry).
  const [attachTarget, setAttachTarget] = useState<string | null>(null);
  const [liveTranscript, setLiveTranscript] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const fileRecording = useRef(false);
  const cancelled = useRef(false);

  useEffect(() => {
    if (!entryId) return;
    let cancelled = false;
    getEntry(entryId)
      .then((entry) => {
        if (cancelled) return;
        if (entry.status === 'in_progress' || entry.status === 'needs_processing') {
          setAttach(entry.status);
        } else {
          setAttach('settled');
        }
      })
      .catch(() => {
        if (!cancelled) setAttach('missing');
      });
    return () => {
      cancelled = true;
    };
  }, [entryId]);

  const attachReady = attach === null || attach === 'in_progress' || attach === 'needs_processing';

  const stopTimer = () => {
    if (timer.current !== null) {
      clearInterval(timer.current);
      timer.current = null;
    }
  };

  useEffect(
    () => () => {
      cancelled.current = true;
      stopTimer();
      try {
        stream.stop();
      } catch {
        /* stream was never started */
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const begin = async () => {
    setError(null);
    try {
      await ensureMicPermission();
      await stream.start();
      await stream.startFileRecordingAsync({ format: 'wav' });
      fileRecording.current = true;
      const started = Date.now();
      setSeconds(0);
      timer.current = setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 250);
      setPhase({ name: 'recording' });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Microphone unavailable.');
      try {
        stream.stop();
      } catch {
        /* already stopped */
      }
    }
  };

  const finishRecording = async () => {
    stopTimer();
    setPhase({ name: 'working', label: 'Finishing the recording…' });
    try {
      let audioUri: string | null = null;
      if (fileRecording.current) {
        fileRecording.current = false;
        const result = await stream.stopFileRecordingAsync();
        audioUri = await ensureWav16k(result);
      }
      try {
        stream.stop();
      } catch {
        /* already stopped */
      }
      if (!audioUri) throw new Error('Nothing was recorded. Try again.');
      const recording = { uri: audioUri, recordingId: newRecordingId() };
      setPendingAudio(recording);
      await uploadPending(recording);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save. Try again.');
      setPhase({ name: 'ready' });
    }
  };

  /** Upload a finished recording (first try or retry): the recording id is
   * stable, so a retried upload replays instead of duplicating. */
  const uploadPending = async (recording: { uri: string; recordingId: string }) => {
    setError(null);
    setPhase({ name: 'working', label: 'Saving…' });
    try {
      const entry = entryId
        ? await attachNote(entryId, {
            audioUri: recording.uri,
            recordingId: recording.recordingId,
          })
        : await saveAttack({
            audioUri: recording.uri,
            recordingId: recording.recordingId,
            coords: await getLogCoords(),
          });
      thump();
      setPendingAudio(null); // the server holds it from here
      await understand(entry);
    } catch (err) {
      // Upload failed: keep the recording for "Retry upload".
      setError(err instanceof Error ? err.message : 'Could not save. Try again.');
      setPhase({ name: 'ready' });
    }
  };

  const saveTyped = async () => {
    if (!typed.trim()) return;
    setError(null);
    setPhase({ name: 'working', label: 'Saving…' });
    try {
      const target = entryId ?? attachTarget;
      const entry = target
        ? await attachNote(target, { transcript: typed.trim() })
        : await saveAttack({ transcript: typed.trim(), coords: await getLogCoords() });
      thump();
      setTyped('');
      setAttachTarget(null);
      await understand(entry);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save. Try again.');
      setPhase({ name: 'typing' });
    }
  };

  const onVoiceProgress = (progress: VoiceProgress) => {
    if (cancelled.current) return;
    if (progress.entry.transcript?.trim()) {
      setLiveTranscript(progress.entry.transcript);
    }
    if (progress.phase === 'transcribing') {
      setPhase({ name: 'working', label: 'Transcribing…' });
    } else if (progress.phase === 'transcript-ready') {
      setPhase({ name: 'working', label: 'Transcript ready. Organizing details…' });
    } else if (progress.phase === 'organizing') {
      setPhase({ name: 'working', label: 'Organizing details…' });
    } else if (progress.phase === 'done') {
      const running = progress.entry.status === 'in_progress';
      setPhase({
        name: 'done',
        entry: progress.entry,
        understood: true,
        detail: null,
        running,
      });
    } else {
      const running = progress.entry.status === 'in_progress';
      setPhase({
        name: 'done',
        entry: progress.entry,
        understood: false,
        detail: progress.detail ?? 'Unknown error.',
        running,
      });
    }
  };

  const understand = async (entry: Entry) => {
    setPhase({ name: 'working', label: 'Saved. Transcribing…' });
    setLiveTranscript(entry.transcript?.trim() ? entry.transcript : null);
    try {
      await recoverEntry(entry.id, await getLogCoords(), onVoiceProgress, {
        cancelled: () => cancelled.current,
      });
    } catch (err) {
      if (cancelled.current) return;
      setPhase({
        name: 'done',
        entry,
        understood: false,
        detail: err instanceof Error ? err.message : 'Unknown error.',
        running: entry.status === 'in_progress',
      });
    }
  };

  const retry = async (entry: Entry) => {
    setPhase({ name: 'working', label: 'Trying again…' });
    await understand(entry);
  };

  const typeInstead = (entry: Entry) => {
    setAttachTarget(entry.id);
    setError(null);
    setPhase({ name: 'typing' });
  };

  const submitAnswers = async (entry: Entry, gaps: Followup[]) => {
    const pairs = gaps
      .filter((gap) => answers[gap.id]?.trim())
      .map((gap) => ({ question: gap.question, answer: (answers[gap.id] ?? '').trim() }));
    if (pairs.length === 0) {
      setAskedRound(true);
      return;
    }
    setAnswering(true);
    setError(null);
    try {
      const updated = await answerFollowups(entry.id, pairs);
      setAskedRound(true);
      setAnswers({});
      await understand(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save answers. Try again.');
    } finally {
      setAnswering(false);
    }
  };

  return (
    <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={styles.column}>
      {entryId && attachReady && (phase.name === 'ready' || phase.name === 'typing') ? (
        <Text style={styles.attachTitle}>Adding details to your attack</Text>
      ) : null}
      {attach === 'loading' ? <Text style={styles.center}>Loading…</Text> : null}
      {attach === 'missing' ? (
        <>
          <ErrorText>Could not load this attack.</ErrorText>
          <GhostButton title="Back" onPress={onHome} />
        </>
      ) : null}
      {attach === 'settled' ? (
        <>
          <Text style={styles.center}>This note is already understood. Edit it in History.</Text>
          <GhostButton title="Back" onPress={onHome} />
        </>
      ) : null}

      {phase.name === 'ready' && attachReady && (
        <>
          <ScalePressable
            onPress={begin}
            accessibilityRole="button"
            accessibilityLabel="Start recording"
            style={styles.record}
          >
            <View style={styles.dot} />
            <Text style={styles.recordTitle}>Record</Text>
            <Text style={styles.recordHint}>tap, speak, tap to stop</Text>
          </ScalePressable>
          {pendingAudio ? (
            <PrimaryButton title="Retry upload" onPress={() => uploadPending(pendingAudio)} />
          ) : null}
          <GhostButton title="Type instead" onPress={() => { setError(null); setPhase({ name: 'typing' }); }} />
        </>
      )}

      {phase.name === 'recording' && (
        <>
          <Text accessibilityLabel={`${seconds} seconds recorded`} style={styles.clock}>
            {formatClock(seconds)}
          </Text>
          <ScalePressable
            onPress={finishRecording}
            accessibilityRole="button"
            accessibilityLabel="Stop recording and save"
            style={styles.stop}
          >
            <View style={styles.square} />
            <Text style={styles.stopText}>Stop and save</Text>
          </ScalePressable>
        </>
      )}

      {phase.name === 'typing' && attachReady && (
        <>
          <Text style={styles.label}>What happened, in your own words?</Text>
          <TextInput
            value={typed}
            onChangeText={setTyped}
            multiline
            numberOfLines={4}
            placeholder="Migraine since lunch, nausea, took ibuprofen…"
            placeholderTextColor={colors.dim}
            style={styles.input}
            textAlignVertical="top"
          />
          <PrimaryButton title="Save note" onPress={saveTyped} disabled={!typed.trim()} />
          <GhostButton title="Back to recording" onPress={() => { setError(null); setPhase({ name: 'ready' }); }} />
        </>
      )}

      {phase.name === 'working' && (
        <View style={styles.statusBox}>
          <Text style={styles.statusText}>{phase.label}</Text>
          {liveTranscript ? <Text style={styles.transcript}>{liveTranscript}</Text> : null}
        </View>
      )}

      {phase.name === 'done' && (
        <>
          <View style={styles.savedBox}>
            <Text style={styles.savedText}>Saved</Text>
          </View>
          {phase.running && phase.understood ? (
            <>
              <Text style={styles.center}>
                Note saved. Your attack is still running — end it from Home.
              </Text>
              {phase.entry.transcript ? (
                <Text style={styles.transcript}>{phase.entry.transcript}</Text>
              ) : null}
            </>
          ) : phase.understood ? (
            <>
              <Text style={styles.center}>Your note is ready to review.</Text>
              <FollowupCard
                entry={phase.entry}
                askedRound={askedRound}
                answers={answers}
                answering={answering}
                onAnswer={(id, value) => setAnswers((prev) => ({ ...prev, [id]: value }))}
                onSkip={() => setAskedRound(true)}
                onSubmit={(gaps) => submitAnswers(phase.entry, gaps)}
              />
            </>
          ) : (
            <View style={styles.retryBox}>
              <Text style={styles.body}>
                {phase.entry.transcript
                  ? 'Transcript ready, but the details aren\u2019t organized yet.'
                  : 'Saved, but not understood yet.'}
              </Text>
              {phase.entry.transcript ? (
                <Text style={styles.transcript}>{phase.entry.transcript}</Text>
              ) : null}
              {phase.detail ? <Text style={styles.dim}>{phase.detail}</Text> : null}
              <View style={styles.retryGap}>
                <GhostButton
                  title={phase.entry.transcript ? 'Try again' : 'Retry transcription'}
                  onPress={() => retry(phase.entry)}
                />
                <GhostButton title="Type instead" onPress={() => typeInstead(phase.entry)} />
              </View>
            </View>
          )}
          <GhostButton title="Review this note" onPress={() => onReviewEntry(phase.entry.id)} />
          <GhostButton title="Back" onPress={onHome} />
        </>
      )}

      {error ? <ErrorText>{error}</ErrorText> : null}

      {(phase.name === 'ready' || phase.name === 'typing') && attachReady && (
        <GhostButton title="Back" onPress={onHome} />
      )}
    </KeyboardAvoidingView>
  );
}

function FollowupCard({
  entry,
  askedRound,
  answers,
  answering,
  onAnswer,
  onSkip,
  onSubmit,
}: {
  entry: Entry;
  askedRound: boolean;
  answers: Record<string, string>;
  answering: boolean;
  onAnswer: (id: string, value: string) => void;
  onSkip: () => void;
  onSubmit: (gaps: Followup[]) => void;
}) {
  const gaps = !askedRound && entry.status === 'unconfirmed' ? followupsFor(entry) : [];
  if (gaps.length === 0) return null;
  const ready = gaps.some((gap) => answers[gap.id]?.trim());
  return (
    <View style={styles.followCard}>
      <Text style={styles.followTitle}>
        {gaps.length === 1 ? 'One quick question' : 'Two quick questions'}
      </Text>
      {gaps.map((gap) => (
        <View key={gap.id} style={styles.followGap}>
          <Text style={styles.followQ}>{gap.question}</Text>
          {gap.kind === 'chips' ? (
            <View style={styles.chipRow}>
              {(gap.options ?? []).map((option) => {
                const selected = answers[gap.id] === option;
                return (
                  <ScalePressable
                    key={option}
                    onPress={() => onAnswer(gap.id, option)}
                    accessibilityRole="button"
                    accessibilityState={{ selected }}
                    style={[styles.chip, selected && styles.chipSelected]}
                  >
                    <Text style={selected ? styles.chipTextSelected : styles.chipText}>
                      {option}
                    </Text>
                  </ScalePressable>
                );
              })}
            </View>
          ) : (
            <TextInput
              value={answers[gap.id] ?? ''}
              onChangeText={(value) => onAnswer(gap.id, value)}
              placeholder="Your answer"
              placeholderTextColor={colors.dim}
              style={styles.followInput}
            />
          )}
        </View>
      ))}
      <PrimaryButton
        title={answering ? 'Saving…' : 'Save answers'}
        onPress={() => onSubmit(gaps)}
        disabled={answering || !ready}
      />
      <GhostButton title="Skip" onPress={onSkip} />
    </View>
  );
}

export default function Capture() {
  return (
    <ScreenShell topPad>
      <CaptureBody />
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  column: { flexDirection: 'column', gap: 18, paddingTop: 24 },
  attachTitle: { color: colors.sand, fontSize: 20, fontFamily: fonts.semibold },
  record: {
    minHeight: 160,
    borderRadius: sizes.radius,
    backgroundColor: colors.gold,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 8,
  },
  dot: { width: 32, height: 32, borderRadius: 16, backgroundColor: colors.ink },
  recordTitle: { color: colors.ink, fontSize: 26, fontFamily: fonts.display },
  recordHint: { color: colors.ink, fontSize: sizes.body, opacity: 0.75 },
  clock: { color: colors.sand, fontSize: 48, fontFamily: fonts.semibold, textAlign: 'center', fontVariant: ['tabular-nums'] },
  stop: {
    minHeight: 160,
    borderRadius: sizes.radius,
    borderWidth: 2,
    borderColor: colors.gold,
    backgroundColor: colors.panelRaised,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 8,
  },
  square: { width: 28, height: 28, borderRadius: 6, backgroundColor: colors.gold },
  stopText: { color: colors.sand, fontSize: 26, fontFamily: fonts.display },
  label: { color: colors.sand, fontSize: 20, fontFamily: fonts.medium },
  input: {
    minHeight: 128,
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panel,
    color: colors.sand,
    fontSize: 20,
    padding: 16,
  },
  statusBox: {
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.edge,
    borderTopColor: colors.rise,
    backgroundColor: colors.panel,
    paddingHorizontal: 20,
    paddingVertical: 40,
    ...shadows.card,
  },
  statusText: { color: colors.sand, fontSize: 22, textAlign: 'center' },
  savedBox: {   borderRadius: sizes.radius, backgroundColor: colors.sage, paddingVertical: 32 },
  savedText: { color: colors.ink, fontSize: 32, fontFamily: fonts.display, textAlign: 'center' },
  center: { color: colors.sand, fontSize: 20, textAlign: 'center' },
  retryBox: {
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.edge,
    borderTopColor: colors.rise,
    backgroundColor: colors.panel,
    padding: 18,
    ...shadows.card,
  },
  body: { color: colors.sand, fontSize: 20 },
  dim: { color: colors.dim, fontSize: 16, marginTop: 4 },
  transcript: {
    color: colors.sand,
    fontSize: sizes.body,
    marginTop: 12,
    padding: 14,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.night,
  },
  retryGap: { marginTop: 12 },
  followCard: {
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.edge,
    borderTopColor: colors.rise,
    backgroundColor: colors.panel,
    padding: 18,
    flexDirection: 'column',
    gap: 14,
    ...shadows.card,
  },
  followTitle: { color: colors.sand, fontSize: 20, fontFamily: fonts.semibold },
  followGap: { flexDirection: 'column', gap: 10 },
  followQ: { color: colors.sand, fontSize: sizes.body, fontFamily: fonts.medium },
  followInput: {
    minHeight: sizes.touch,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.night,
    color: colors.sand,
    fontSize: sizes.body,
    paddingHorizontal: 14,
  },
  chipRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  chip: {
    minHeight: 48,
    paddingHorizontal: 16,
    justifyContent: 'center',
    borderRadius: 24,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.night,
  },
  chipSelected: { borderColor: colors.lightAmber, backgroundColor: colors.lightAmber },
  chipText: { color: colors.sand, fontSize: 16 },
  chipTextSelected: { color: colors.ink, fontSize: 16, fontFamily: fonts.semibold },
});
