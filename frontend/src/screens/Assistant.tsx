import { useEffect, useRef, useState, type ComponentRef, type ReactNode } from 'react';
import {
  Animated,
  FlatList,
  Platform,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { KeyboardAvoidingView, KeyboardProvider } from 'react-native-keyboard-controller';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';
import { useNavigation } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import {
  Activity,
  ChartLine,
  Check,
  MessageSquarePlus,
  Sparkles,
  Timer,
  X,
  type LucideIcon,
} from 'lucide-react-native';
import { printToFileAsync } from 'expo-print';
import { isAvailableAsync, shareAsync } from 'expo-sharing';
import { agentChat, getAgentTurn, receiptFromEntry, formatDay, answerFollowups, processEntry, listEntries, draftNarrative, getSummary, startAttack, endAttack, saveCheckin, ApiError, TurnExpiredError, type AgentAction, type AgentResponse } from '../api';
import { ensureMicPermission, ensureWav16k, formatClock, useAudioStream } from '../audio';
import { useChat, type ChatMessage } from '../chat';
import { isPermanentFailure, transcribeWithRetry } from '../voice';
import { thump, tick } from '../haptics';
import { getLogCoords } from '../location';
import { useReducedMotionSetting } from '../motion';
import { buildSummaryHtml } from '../print';
import { colors, fonts, shadows, sizes } from '../theme';
import { ErrorText, FilterChip, GhostButton, PrimaryButton, ScalePressable } from '../components/ui';
import AssistantComposer from '../components/AssistantComposer';
import type { RootStackParamList } from '../nav';

const EMPTY_HINT = 'Log how you feel, or ask about your diary.';

/** Deterministic quick actions: no model round-trip, straight to the API. */
type QuickAction = 'start-timer' | 'end-timer' | 'checkin';

interface ChipAction {
  label: string;
  message?: string;
  reviewId?: string;
  quick?: QuickAction;
}

const PROGRESS_STAGES = ['Thinking…', 'Working on it…', 'Still going…'];

const LOG_TOOLS = new Set(['log_attack', 'add_note', 'log_checkin', 'timer_start', 'timer_end', 'set_entry_date', 'update_entry']);

/** Random hex id (uniqueness per send, not security): no dependency needed. */
function newRequestId(): string {
  const bytes = new Uint8Array(16);
  for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function toTranscript(msgs: Array<{ role: 'user' | 'assistant'; text: string; actions?: AgentAction[] }>): Array<{ role: 'user' | 'assistant'; content: string }> {
  return msgs.slice(-10).map((m) => ({
    role: m.role,
    content: m.text +
      (m.actions?.length ? `\nAction receipts: ${JSON.stringify(m.actions)}` : ''),
  }));
}

function ActionChips({
  chips,
  onAct,
  disabled,
}: {
  disabled: boolean;
  chips: ChipAction[];
  onAct: (chip: ChipAction) => void;
}) {
  return (
    <View style={styles.chipRow}>
      {chips.map((chip) => (
        <FilterChip
          disabled={disabled}
          key={chip.label}
          label={chip.label}
          onPress={() => onAct(chip)}
        />
      ))}
    </View>
  );
}

/** Contextual chips: review what was just logged, end a running timer, else presets. */
function chipsFor(
  messages: ChatMessage[],
  timerRunning: boolean,
  pending: boolean,
): ChipAction[] | null {
  if (pending) return null;
  const last = [...messages].reverse().find((m) => m.role === 'assistant');
  const acts = last?.actions ?? [];
  const chips: ChipAction[] = [];
  const reviewable = acts.find(
    (a) => a.ok && a.entry_id && (LOG_TOOLS.has(a.tool) || a.tool === 'answer_followups' || a.tool === 'get_entry'),
  );
  if (reviewable?.entry_id) {
    chips.push({ label: 'Review it', reviewId: reviewable.entry_id });
  }
  if (timerRunning) chips.push({ label: 'End timer', quick: 'end-timer' });
  const fill: ChipAction[] = [
    { label: 'Log a headache', message: 'I have a headache.' },
    { label: 'No headache today', quick: 'checkin' },
    ...(timerRunning ? [] : [{ label: 'Start timer', quick: 'start-timer' } as ChipAction]),
    { label: 'Ask about my month', message: 'How many migraines did I have this month?' },
  ];
  for (const chip of fill) {
    if (chips.length >= 4) break;
    chips.push(chip);
  }
  return chips;
}

function readDetail(action: AgentAction, onReview: (id: string) => void): ReactNode {
  const data = action.data ?? {};
  if (action.tool === 'timer_status') {
    if (data.running === true) {
      return (
        <Text style={styles.receiptDetail}>
          Running{typeof data.started === 'string' ? ` since ${data.started}` : ''}
        </Text>
      );
    }
    return <Text style={styles.receiptDetail}>Not running</Text>;
  }
  if (action.tool === 'answer_diary' && typeof data.answer === 'string') {
    const citations = data.citations ?? [];
    return (
      <>
        <Text numberOfLines={4} style={styles.receiptQuote}>{data.answer}</Text>
        {citations.length > 0 ? (
          <View style={styles.chipWrap}>
            {citations.map((cite) => (
              <FilterChip
                key={cite.entry_id}
                label={`${cite.date ? formatDay(cite.date) : 'Entry'}${typeof cite.pain === 'number' ? ` · Pain ${cite.pain}/10` : ''}`}
                onPress={() => onReview(cite.entry_id)}
                accessibilityLabel={`Open entry from ${cite.date ? formatDay(cite.date) : 'unknown date'}`}
                style={styles.citationChip}
              />
            ))}
          </View>
        ) : null}
      </>
    );
  }
  if (action.tool === 'get_summary' && typeof data.attack_entries === 'number') {
    return (
      <Text style={styles.receiptDetail}>
        {data.attack_entries} attack{data.attack_entries === 1 ? '' : 's'}
        {typeof data.window_days === 'number' ? ` · last ${data.window_days} days` : ''}
      </Text>
    );
  }
  if (action.tool === 'draft_narrative' && typeof data.narrative === 'string') {
    return <Text numberOfLines={4} style={styles.receiptQuote}>{data.narrative}</Text>;
  }
  if (action.tool === 'find_entries' && typeof data.total === 'number') {
    return <Text style={styles.receiptDetail}>{data.total} matching entries</Text>;
  }
  return null;
}

function AssistantBubble({
  message,
  reduceMotion,
  onReview,
}: {
  message: ChatMessage;
  reduceMotion: boolean;
  onReview: (id: string) => void;
}) {
  const pop = useRef(new Animated.Value(reduceMotion ? 1 : 0)).current;
  useEffect(() => {
    if (reduceMotion) return;
    Animated.spring(pop, { toValue: 1, useNativeDriver: true }).start();
  }, [pop, reduceMotion]);
  return (
    <Animated.View
      style={[
        styles.assistantBubble,
        !reduceMotion && {
          opacity: pop,
          transform: [{ scale: pop.interpolate({ inputRange: [0, 1], outputRange: [0.94, 1] }) }],
        },
      ]}
    >
      <Text selectable style={styles.assistantText}>{message.text}</Text>
      {(message.actions ?? []).map((action, index) => {
        const receipt = action.data?.receipt;
        const status = receipt?.status === 'in_progress' ? 'Timer running · details saved'
          : receipt?.status === 'needs_processing' ? (receipt.transcript ? 'Saved · processing needs retry' : 'Saved · add a note when ready')
          : receipt?.status === 'confirmed' ? 'Saved · confirmed'
          : 'Saved · ready to review';
        return (
          <View key={`${action.tool}-${index}`} style={styles.receiptCard}>
            <Text style={styles.receiptTitle}>{action.ok ? '✓' : '✗'} {action.label}</Text>
            {receipt ? <>
              <Text style={styles.receiptMeta}>{receipt.date ? formatDay(receipt.date) : ''} · {status}</Text>
              {receipt.start ? <Text style={styles.receiptDetail}>Started {new Date(receipt.start).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}</Text> : null}
              {typeof receipt.pain === 'number' ? <Text style={styles.receiptDetail}>Pain {receipt.pain}/10</Text> : null}
              {receipt.medications?.length ? <Text style={styles.receiptDetail}>{receipt.medications.join(', ')}</Text> : null}
              {receipt.duration_minutes != null ? <Text style={styles.receiptDetail}>Duration {receipt.duration_minutes} min</Text> : null}
              {receipt.transcript ? <Text numberOfLines={3} style={styles.receiptQuote}>“{receipt.transcript}”</Text> : null}
            </> : null}
            {!receipt ? readDetail(action, onReview) : null}
            {action.entry_id ? (
              <ScalePressable onPress={() => onReview(action.entry_id as string)}
                accessibilityRole="button" accessibilityLabel="View and edit this entry"
                style={styles.reviewButton}>
                <Text style={styles.reviewLink}>Edit entry →</Text>
              </ScalePressable>
            ) : null}
          </View>
        );
      })}
    </Animated.View>
  );
}

export default function Assistant() {
  // Keep keyboard tracking inside the full-screen modal and its lifecycle.
  return (
    <KeyboardProvider>
      <AssistantScreen />
    </KeyboardProvider>
  );
}

function AssistantScreen() {
  const navigation = useNavigation<NativeStackNavigationProp<RootStackParamList>>();
  const insets = useSafeAreaInsets();
  const { messages, appendMessage, updateMessage, clearMessages, setJustLoggedId, pending, setPending, followAnswers, setFollowAnswers, input, setInput, busy, setBusy, sending, turnState, setTurnState } = useChat();

  const [error, setError] = useState<string | null>(null);
  const [timerRunning, setTimerRunning] = useState(false);
  const [elapsed, setElapsed] = useState<string | null>(null);
  const newSpin = useRef(new Animated.Value(0)).current;

  // Live elapsed clock for the header while an attack timer runs: fetch the
  // running entry once per run, then tick locally (no polling).
  useEffect(() => {
    if (!timerRunning) {
      setElapsed(null);
      return;
    }
    let cancelled = false;
    let interval: ReturnType<typeof setInterval> | null = null;
    listEntries({ status: 'in_progress', entry_type: 'attack', limit: 1 })
      .then((mine) => {
        if (cancelled) return;
        const running = mine.entries[0];
        const startIso = running?.attack?.attack_start ?? running?.created_at;
        if (!startIso) return;
        const startMs = new Date(startIso).getTime();
        if (!Number.isFinite(startMs)) return;
        const update = () =>
          setElapsed(formatClock(Math.max(0, Math.floor((Date.now() - startMs) / 1000))));
        update();
        interval = setInterval(update, 1000);
      })
      .catch(() => { /* header falls back to the plain label */ });
    return () => {
      cancelled = true;
      if (interval !== null) clearInterval(interval);
    };
  }, [timerRunning]);
  const [progressIdx, setProgressIdx] = useState(0);
  const [pdfStatus, setPdfStatus] = useState<string | null>(null);
  const [quickStatus, setQuickStatus] = useState<string | null>(null);
  const voiceStarting = useRef(false);
  const [startingVoice, setStartingVoice] = useState(false);
  const [recording, setRecording] = useState(false);
  const [recSeconds, setRecSeconds] = useState(0);
  const [transcribing, setTranscribing] = useState(false);
  // Failed chat recording, retained with its error: retry reuses the SAME
  // audio instead of asking to record again.
  const [failedVoice, setFailedVoice] = useState<{
    uri: string;
    detail: string;
    permanent: boolean;
  } | null>(null);
  const list = useRef<FlatList<ChatMessage>>(null);
  const composerInput = useRef<ComponentRef<typeof TextInput>>(null);
  const reduceMotion = useReducedMotionSetting();
  const { stream } = useAudioStream({ sampleRate: 16000, channels: 1, encoding: 'int16' });
  const fileRecording = useRef(false);
  const recTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const nearBottom = useRef(true);
  const unavailable = busy || recording || transcribing || startingVoice;

  // Staged progress: model turns take seconds, so the status line advances
  // instead of sitting on one dead "Thinking…".
  useEffect(() => {
    if (!busy) {
      setProgressIdx(0);
      return;
    }
    const first = setTimeout(() => setProgressIdx(1), 4000);
    const second = setTimeout(() => setProgressIdx(2), 9000);
    return () => {
      clearTimeout(first);
      clearTimeout(second);
    };
  }, [busy]);

  useEffect(
    () => () => {
      if (recTimer.current !== null) clearInterval(recTimer.current);
      try {
        stream.stop();
      } catch {
        /* stream was never started */
      }
    },
    [],
  );

  useEffect(() => {
    let active = true;
    void listEntries({ status: 'in_progress', entry_type: 'attack' })
      .then(({ entries }) => { if (active) setTimerRunning(entries.length > 0); })
      .catch(() => {});
    return () => { active = false; };
  }, []);

  const runPdf = async (windowDays: number) => {
    if (windowDays !== 30 && windowDays !== 60 && windowDays !== 90) return;
    setPdfStatus('Making your PDF…');
    try {
      const summary = await getSummary(windowDays);
      let narrative: string | null = null;
      try {
        narrative = (await draftNarrative(windowDays)).narrative;
      } catch {
        narrative = null;
      }
      const file = await printToFileAsync({ html: buildSummaryHtml(summary, narrative) });
      if (!(await isAvailableAsync())) {
        appendMessage({
          role: 'assistant',
          text: 'Your PDF is ready, but sharing is not available on this device.',
        });
        return;
      }
      await shareAsync(file.uri);
      appendMessage({
        role: 'assistant',
        text: 'Report prepared. You can create another copy from Reports.',
      });
    } catch {
      appendMessage({
        role: 'assistant',
        text: 'I could not make that PDF. Try again, or make it from Reports.',
      });
    } finally {
      setPdfStatus(null);
    }
  };

  const send = async (raw?: string) => {
    const text = (raw ?? input).trim();
    if (!text || sending.current || recording || transcribing || voiceStarting.current) return;
    sending.current = true;
    nearBottom.current = true;
    tick();
    const requestId = newRequestId();
    const messageId = appendMessage({ role: 'user', text, requestId, sendState: 'sending' });
    const transcript = toTranscript([...messages, { role: 'user' as const, text }]);



    setInput('');
    setBusy(true);
    setError(null);
    try {
      const coords = await getLogCoords();
      let result: AgentResponse;
      try {
        result = await agentChat(text, transcript.slice(0, -1), coords, { requestId, context: turnState });
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          result = await pollTurn(requestId);
        } else {
          throw err;
        }
      }
      updateMessage(messageId, { sendState: 'sent' });
      setTurnState(result.state ?? null);
      appendMessage({ role: 'assistant', text: result.reply, actions: result.actions });
      setTimerRunning(result.state?.timer_running ?? false);
      if (result.actions.some((a) => a.ok && LOG_TOOLS.has(a.tool))) thump();
      const pdf = result.actions.find((a) => a.ok && a.tool === 'save_pdf');
      if (pdf) await runPdf(Number(pdf.data?.window_days));
      const answered = result.actions.find((a) => a.ok && a.tool === 'answer_followups');
      const logged = [...result.actions].reverse().find(
        (a) => a.ok && a.entry_id && LOG_TOOLS.has(a.tool),
      );
      if (answered?.entry_id) {
        setPending(null);
        setJustLoggedId(answered.entry_id);
      } else if (logged?.entry_id) {
        const followups = logged.followups ?? [];
        if (followups.length > 0) {
          setFollowAnswers({});
          setPending({ entryId: logged.entry_id, followups });
        } else {
          setJustLoggedId(logged.entry_id);
        }
      }
    } catch (err) {
      failMessage(messageId, err);
    } finally {
      sending.current = false;
      setBusy(false);
    }
  };

  const failMessage = (messageId: string, err: unknown) => {
    if (err instanceof TurnExpiredError) {
      updateMessage(messageId, { sendState: 'failed', failureKind: 'unknown' });
    } else if (err instanceof ApiError && err.status === 422) {
      updateMessage(messageId, { sendState: 'failed', failureKind: 'rejected' });
    } else {
      updateMessage(messageId, { sendState: 'failed' });
    }
    setError(err instanceof Error ? err.message : 'Could not reach the assistant. Tap the message to retry.');
  };

  const pollTurn = async (requestId: string): Promise<AgentResponse> => {
    for (let attempt = 0; attempt < 12; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 700));
      const turn = await getAgentTurn(requestId);
      if (turn.status === 'completed' && turn.response) return turn.response;
      if (turn.status === 'failed') {
        throw new Error('That turn failed on the server. Tap the message to retry.');
      }
      if (turn.status === 'expired') {
        throw new TurnExpiredError('That turn could not be confirmed — it may already have saved. Check History before sending again.');
      }
      setProgressIdx((attempt + 1) % PROGRESS_STAGES.length);
    }
    throw new Error('Still working on it. Tap the message to pick up the result.');
  };

  const retry = async (messageId: string) => {
    const message = messages.find((m) => m.id === messageId);
    if (!message || message.role !== 'user' || sending.current || unavailable) return;
    const requestId = message.requestId ?? newRequestId();
    const index = messages.findIndex((m) => m.id === messageId);
    const transcript = toTranscript(messages.slice(0, index + 1));
    sending.current = true;
    tick();
    updateMessage(messageId, { sendState: 'sending', requestId });
    setBusy(true);
    setError(null);
    try {
      const coords = await getLogCoords();
      let result: AgentResponse;
      try {
        result = await agentChat(message.text, transcript.slice(0, -1), coords, { requestId, context: turnState });
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          result = await pollTurn(requestId);
        } else {
          throw err;
        }
      }
      updateMessage(messageId, { sendState: 'sent' });
      setTurnState(result.state ?? null);
      appendMessage({ role: 'assistant', text: result.reply, actions: result.actions });
      setTimerRunning(result.state?.timer_running ?? false);
      if (result.actions.some((a) => a.ok && LOG_TOOLS.has(a.tool))) thump();
      const logged = [...result.actions].reverse().find(
        (a) => a.ok && a.entry_id && LOG_TOOLS.has(a.tool),
      );
      if (logged?.entry_id) {
        const followups = logged.followups ?? [];
        if (followups.length > 0) {
          setFollowAnswers({});
          setPending({ entryId: logged.entry_id, followups });
        } else {
          setJustLoggedId(logged.entry_id);
        }
      }
    } catch (err) {
      failMessage(messageId, err);
    } finally {
      sending.current = false;
      setBusy(false);
    }
  };

  const beginVoice = async () => {
    if (sending.current || recording || transcribing || voiceStarting.current) return;
    voiceStarting.current = true;
    setStartingVoice(true);
    setError(null);
    try {
      await ensureMicPermission();
      await stream.start();
      await stream.startFileRecordingAsync({ format: 'wav' });
      fileRecording.current = true;
      const started = Date.now();
      setRecSeconds(0);
      recTimer.current = setInterval(
        () => setRecSeconds(Math.floor((Date.now() - started) / 1000)),
        250,
      );
      setRecording(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Microphone unavailable.');
      try {
        stream.stop();
      } catch {
        /* already stopped */
      }
    } finally {
      voiceStarting.current = false;
      setStartingVoice(false);
    }
  };

  const finishVoice = async () => {
    if (!fileRecording.current || transcribing) return;
    let audioUri: string | null = null;
    if (recTimer.current !== null) {
      clearInterval(recTimer.current);
      recTimer.current = null;
    }
    setRecording(false);
    setTranscribing(true);
    try {

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
      setFailedVoice(null);
      const transcript = (await transcribeWithRetry(audioUri)).trim();
      if (!transcript) {
        // Heard nothing: keep the recording for a retry all the same.
        setFailedVoice({
          uri: audioUri,
          detail: 'Could not hear anything in that recording.',
          permanent: false,
        });
      } else {
        setInput(previous => previous.trim() ? `${previous.trim()} ${transcript}` : transcript);
      }
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'Could not transcribe. Try again.';
      if (audioUri) {
        setFailedVoice({ uri: audioUri, detail, permanent: isPermanentFailure(err) });
      } else {
        setError(detail);
      }
    } finally {
      try { stream.stop(); } catch { /* already stopped */ }
      setTranscribing(false);
    }
  };

  const retryVoice = async () => {
    if (!failedVoice || transcribing || recording) return;
    setTranscribing(true);
    setError(null);
    try {
      const transcript = (await transcribeWithRetry(failedVoice.uri)).trim();
      if (!transcript) {
        setFailedVoice({
          ...failedVoice,
          detail: 'Could not hear anything in that recording.',
        });
        return;
      }
      setFailedVoice(null);
      setInput(previous => previous.trim() ? `${previous.trim()} ${transcript}` : transcript);
    } catch (err) {
      setFailedVoice({
        uri: failedVoice.uri,
        detail: err instanceof Error ? err.message : 'Could not transcribe. Try again.',
        permanent: isPermanentFailure(err),
      });
    } finally {
      setTranscribing(false);
    }
  };

  const typeInsteadVoice = () => {
    setFailedVoice(null);
    setError(null);
    composerInput.current?.focus();
  };

  const submitFollowAnswers = async () => {
    if (!pending || sending.current || unavailable) return;
    const pairs = pending.followups
      .filter((gap) => followAnswers[gap.id]?.trim())
      .map((gap) => ({ question: gap.question, answer: followAnswers[gap.id].trim() }));
    if (!pairs.length) return;
    sending.current = true;
    setBusy(true);
    setError(null);
    const entryId = pending.entryId;
    try {
      const saved = await answerFollowups(entryId, pairs);
      // Clear only after saving, so a failed request preserves the user's answers.
      setPending(null);
      setFollowAnswers({});
      appendMessage({ role: 'user', text: pairs.map(p => `${p.question} ${p.answer}`).join('\n') });
      let receipt = receiptFromEntry(saved);
      let text = 'Answers saved. Review your updated note when you’re ready.';
      try {
        const entry = await processEntry(entryId);
        receipt = receiptFromEntry(entry);
        if (entry.status === 'needs_processing') text = 'Answers saved. Processing needs a retry from the entry.';
      } catch {
        text = 'Answers saved. Processing needs a retry from the entry.';
      }
      appendMessage({ role: 'assistant', text, actions: [{ tool: 'answer_followups', ok: true, label: 'Answers saved', entry_id: entryId, data: { receipt } }] });
      setJustLoggedId(entryId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save answers. Your answers are still here.');
    } finally {
      sending.current = false;
      setBusy(false);
    }
  };

  const skipFollowups = () => {
    if (!pending || sending.current || unavailable) return;
    setJustLoggedId(pending.entryId);
    setPending(null);
    setFollowAnswers({});
    appendMessage({ role: 'assistant', text: 'Your note is saved. You can add details later from History.' });
  };

  const spinRotate = newSpin.interpolate({
    inputRange: [0, 1],
    outputRange: ['0deg', '360deg'],
  });

  const spinNewChat = () => {
    if (unavailable) return;
    if (!reduceMotion) {
      newSpin.setValue(0);
      Animated.timing(newSpin, { toValue: 1, duration: 350, useNativeDriver: true }).start();
    }
    newChat();
  };

  const newChat = () => {
    if (unavailable) return;
    tick();
    clearMessages();
    setTurnState(null);
    setPending(null);
    setFollowAnswers({});
    setInput('');
    setError(null);
  };

  /**
   * Deterministic quick actions: start/end timer and check-in go straight to
   * the API (no model round-trip), then land in the transcript as ordinary
   * messages with real receipts, so later model turns see them too. Works
   * while the model is unreachable; failures fall back to typing.
   */
  const runQuick = async (kind: QuickAction, label: string) => {
    if (unavailable || sending.current) return;
    sending.current = true;
    nearBottom.current = true;
    tick();
    appendMessage({ role: 'user', text: label });
    setBusy(true);
    setQuickStatus(
      kind === 'start-timer'
        ? 'Starting timer\u2026'
        : kind === 'end-timer'
          ? 'Ending timer\u2026'
          : 'Saving check-in\u2026',
    );
    setError(null);
    try {
      const coords = await getLogCoords();
      if (kind === 'start-timer') {
        const entry = await startAttack(coords);
        setTimerRunning(true);
        setTurnState({
          ...(turnState ?? { timer_running: false }),
          timer_running: true,
          active_attack_id: entry.id,
        });
        thump();
        appendMessage({
          role: 'assistant',
          text: 'Timer started. Add notes any time \u2014 tap End timer when it\u2019s over.',
          actions: [{
            tool: 'timer_start', ok: true, label: 'Timer started',
            entry_id: entry.id, data: { receipt: receiptFromEntry(entry) },
          }],
        });
      } else if (kind === 'end-timer') {
        const running = await listEntries({ status: 'in_progress', entry_type: 'attack', limit: 1 });
        const target = running.entries[0];
        if (!target) {
          setTimerRunning(false);
          appendMessage({ role: 'assistant', text: 'No timer is running right now.' });
        } else {
          const entry = await endAttack(target.id, coords);
          setTimerRunning(false);
          setTurnState({
            ...(turnState ?? { timer_running: false }),
            timer_running: false,
            active_attack_id: undefined,
          });
          thump();
          setJustLoggedId(entry.id);
          appendMessage({
            role: 'assistant',
            text: entry.status === 'needs_processing' && !entry.transcript
              ? 'Timer ended. Any voice notes are finishing in the background.'
              : 'Timer ended.',
            actions: [{
              tool: 'timer_end', ok: true, label: 'Timer ended',
              entry_id: entry.id, data: { receipt: receiptFromEntry(entry) },
            }],
          });
        }
      } else {
        const entry = await saveCheckin(coords);
        thump();
        appendMessage({
          role: 'assistant',
          text: 'Logged \u2014 no headache today.',
          actions: [{
            tool: 'log_checkin', ok: true, label: 'No-headache check-in',
            entry_id: entry.id, data: { receipt: receiptFromEntry(entry) },
          }],
        });
      }
    } catch (err) {
      appendMessage({
        role: 'assistant',
        text: `Couldn\u2019t do that just now (${err instanceof Error ? err.message : 'unknown error'}). Check the server is running, or type what you need instead.`,
      });
    } finally {
      sending.current = false;
      setBusy(false);
      setQuickStatus(null);
    }
  };

  const actOnChip = (chip: ChipAction) => {
    if (unavailable) return;
    if (chip.quick) {
      void runQuick(chip.quick, chip.label);
      return;
    }
    if (chip.reviewId) {
      navigation.navigate('Detail', { id: chip.reviewId });
      return;
    }
    if (chip.message) void send(chip.message);
  };

  const chips = chipsFor(messages, timerRunning, pending !== null || unavailable);
  const starters: { icon: LucideIcon; label: string; sub: string; chip: ChipAction }[] = [
    {
      icon: Activity,
      label: 'Log a headache',
      sub: 'One tap — details optional',
      chip: { label: 'Log a headache', message: 'I have a headache.' },
    },
    {
      icon: Check,
      label: 'No headache today',
      sub: 'Record today’s check-in',
      chip: { label: 'No headache today', quick: 'checkin' },
    },
    timerRunning
      ? {
          icon: Timer,
          label: 'End timer',
          sub: 'Stop timing this one',
          chip: { label: 'End timer', quick: 'end-timer' },
        }
      : {
          icon: Timer,
          label: 'Start timer',
          sub: 'Time it live',
          chip: { label: 'Start timer', quick: 'start-timer' },
        },
    {
      icon: ChartLine,
      label: 'Ask about my month',
      sub: 'Attacks, pain, patterns',
      chip: { label: 'Ask about my month', message: 'How many migraines did I have this month?' },
    },
  ];

  return (
    <SafeAreaView style={[styles.screen, { paddingTop: insets.top }]} edges={['bottom', 'left', 'right']}>
      {/* Follow native keyboard frames, including interactive dismissal, on the UI thread. */}
      <KeyboardAvoidingView style={styles.sheet} behavior="padding" automaticOffset>
        <View style={styles.header}>
          <View style={styles.titleRow}>
            <View style={styles.titleBadge}>
              <Sparkles size={18} color={colors.ink} />
            </View>
            <View style={styles.headerCopy}>
              <Text style={styles.title}>Quietlog</Text>
              <Text style={styles.subtitle}>{timerRunning ? (elapsed ? `Attack running · ${elapsed}` : 'Attack timer running') : 'Your diary assistant'}</Text>
            </View>
          </View>
          <View style={styles.headerActions}>
            <ScalePressable
              disabled={unavailable}
              onPress={spinNewChat}
              accessibilityRole="button"
              accessibilityLabel="Start a new chat"
              style={styles.newChat}
            >
              <Animated.View style={{ transform: [{ rotate: spinRotate }] }}>
                <MessageSquarePlus size={20} color={colors.sand} />
              </Animated.View>
              <Text style={styles.newChatText}>New</Text>
            </ScalePressable>
            <ScalePressable
              disabled={unavailable}
              onPress={() => navigation.goBack()}
              accessibilityRole="button"
              accessibilityLabel="Close assistant"
              style={styles.close}
            >
              <X size={24} color={colors.sand} />
            </ScalePressable>
          </View>
        </View>
        <View style={styles.frame}>
          <FlatList
            ref={list}
            data={messages}
            keyExtractor={(item) => item.id}
            contentContainerStyle={styles.list}
            keyboardShouldPersistTaps="handled"
            keyboardDismissMode={Platform.OS === 'ios' ? 'interactive' : 'on-drag'}
            onScroll={({ nativeEvent: { contentOffset, contentSize, layoutMeasurement } }) => {
              nearBottom.current = contentSize.height - contentOffset.y - layoutMeasurement.height < 100;
            }}
            scrollEventThrottle={16}
            onContentSizeChange={() => {
              if (nearBottom.current) list.current?.scrollToEnd({ animated: !reduceMotion });
            }}
            ListEmptyComponent={
              <View style={styles.empty}>
                <View style={styles.welcomeBadge}><Sparkles size={26} color={colors.lightAmber} /></View>
                <Text style={styles.eyebrow}>A LITTLE LESS TO REMEMBER</Text>
                <Text style={styles.welcomeTitle}>How are you feeling?</Text>
                <Text style={styles.hint}>{EMPTY_HINT} Type a note or tap the microphone to speak.</Text>
                <Text style={styles.sectionLabel}>Start here</Text>
                {starters.map((starter) => (
                  <ScalePressable
                    key={starter.label}
                    onPress={() => actOnChip(starter.chip)}
                    accessibilityRole="button"
                    accessibilityLabel={starter.label}
                    disabled={unavailable}
                    style={styles.starterCard}
                  >
                    <View style={styles.starterBadge}>
                      <starter.icon size={22} color={colors.gold} />
                    </View>
                    <View style={styles.starterTexts}>
                      <Text style={styles.starterLabel}>{starter.label}</Text>
                      <Text style={styles.starterSub}>{starter.sub}</Text>
                    </View>
                  </ScalePressable>
                ))}
              </View>
            }
            renderItem={({ item }) =>
              item.role === 'user' ? (
                <View style={styles.userRow}>
                  <View style={styles.userBubble}>
                    <Text selectable style={styles.userText}>{item.text}</Text>
                    {item.sendState === 'failed' && !item.failureKind ? (
                      <ScalePressable
                        onPress={() => retry(item.id)}
                        accessibilityRole="button"
                        accessibilityLabel="Retry sending this message"
                        style={styles.retryButton}
                      >
                        <Text style={styles.retryText}>Not sent · Tap to retry</Text>
                      </ScalePressable>
                    ) : null}
                    {item.sendState === 'failed' && item.failureKind === 'unknown' ? (
                      <Text style={styles.retryText}>Couldn’t confirm — check History before resending</Text>
                    ) : null}
                    {item.sendState === 'failed' && item.failureKind === 'rejected' ? (
                      <Text style={styles.retryTextDim}>Not sent</Text>
                    ) : null}
                  </View>
                </View>
              ) : (
                <View style={styles.assistantRow}>
                  <AssistantBubble
                    message={item}
                    reduceMotion={reduceMotion}
                    onReview={(id) => navigation.navigate('Detail', { id })}
                  />
                </View>
              )
            }
            ListFooterComponent={
              <>
                {pending ? (
                  <View style={styles.followCard}>
                    <Text style={styles.followTitle}>
                      {pending.followups.length === 1
                        ? 'One quick question (optional)'
                        : 'Two quick questions (optional)'}
                    </Text>
                    {pending.followups.map((gap) => (
                      <View key={gap.id} style={styles.followGap}>
                        <Text style={styles.followQ}>{gap.question}</Text>
                        {gap.kind === 'chips' ? (
                          <View style={styles.chipWrap}>
                            {(gap.options ?? []).map((option) => {
                              const selected = followAnswers[gap.id] === option;
                              return (
                                <FilterChip
                                  key={option}
                                  label={option}
                                  selected={selected}
                                  disabled={unavailable}
                                  style={styles.followChip}
                                  onPress={() =>
                                    setFollowAnswers((prev) => ({
                                      ...prev,
                                      [gap.id]: selected ? '' : option,
                                    }))
                                  }
                                />
                              );
                            })}
                          </View>
                        ) : (
                          <TextInput
                            value={followAnswers[gap.id] ?? ''}
                            onChangeText={(value) =>
                              setFollowAnswers((prev) => ({ ...prev, [gap.id]: value }))
                            }
                            editable={!unavailable}
                            accessibilityLabel={gap.question}
                            maxLength={1000}
                            placeholder="Your answer"
                            placeholderTextColor={colors.dim}
                            style={styles.followInput}
                          />
                        )}
                      </View>
                    ))}
                    <PrimaryButton
                      title={busy ? 'Saving…' : 'Save answers'}
                      onPress={submitFollowAnswers}
                      disabled={
                        unavailable || !pending.followups.some((gap) => followAnswers[gap.id]?.trim())
                      }
                    />
                    <GhostButton disabled={unavailable} title="Skip for now" onPress={skipFollowups} />
                  </View>
                ) : null}
                {chips && messages.length > 0 ? (
                  <View style={styles.chipsRow}>
                    <ActionChips chips={chips} onAct={actOnChip} disabled={unavailable} />
                  </View>
                ) : null}
              </>
            }
          />
          <View style={styles.frameBottom}>
          {busy ? <Text accessibilityLiveRegion="polite" style={styles.thinking}>{quickStatus ?? pdfStatus ?? PROGRESS_STAGES[progressIdx]}</Text> : null}
          {error ? <ErrorText>{error}</ErrorText> : null}
          {failedVoice && !recording ? (
            <View style={styles.voiceFailBox}>
              <Text style={styles.voiceFailTitle}>Couldn’t transcribe that recording.</Text>
              <Text style={styles.voiceFailDetail}>{failedVoice.detail}</Text>
              <View style={styles.voiceFailRow}>
                <GhostButton
                  title={transcribing ? 'Retrying…' : 'Retry transcription'}
                  onPress={retryVoice}
                  disabled={transcribing}
                />
                <GhostButton title="Type instead" onPress={typeInsteadVoice} />
              </View>
            </View>
          ) : null}
          <AssistantComposer
            ref={composerInput}
            value={input}
            onChangeText={setInput}
            onSend={() => { void send(); }}
            onStartRecording={() => { void beginVoice(); }}
            onStopRecording={() => { void finishVoice(); }}
            busy={busy}
            startingVoice={startingVoice}
            recording={recording}
            recordingSeconds={recSeconds}
            transcribing={transcribing}
          />
          <Text style={styles.disclaimer}>Diary help, never medical advice.</Text>
          </View>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.night },
  sheet: { flex: 1, width: '100%', maxWidth: 760, alignSelf: 'center' },
  headerCopy: { flexShrink: 1, gap: 3 },
  subtitle: { color: colors.dim, fontSize: 13 },
  welcomeBadge: { width: 56, height: 56, borderRadius: sizes.radius, backgroundColor: colors.panelRaised, alignItems: 'center', justifyContent: 'center', marginBottom: 12, ...shadows.card },
  eyebrow: { color: colors.lightAmber, fontSize: 11, fontFamily: fonts.semibold, letterSpacing: 1.6 },
  welcomeTitle: { color: colors.sand, fontFamily: fonts.display, fontSize: 32, lineHeight: 40 },
  sectionLabel: { color: colors.dim, fontSize: 13, fontFamily: fonts.medium, marginTop: 20, marginBottom: 2 },
  receiptCard: { borderTopWidth: 1, borderTopColor: colors.edge, paddingTop: 12, gap: 6 },
  receiptTitle: { color: colors.sand, fontFamily: fonts.semibold, fontSize: 15 },
  receiptMeta: { color: colors.lightAmber, fontSize: 13, lineHeight: 20 },
  receiptDetail: { color: colors.sand, fontSize: 15, lineHeight: 22 },
  receiptQuote: { color: colors.dim, fontSize: 15, lineHeight: 22, marginTop: 4 },
  reviewButton: { minHeight: 56, justifyContent: 'center', paddingHorizontal: 8 },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 20,
    paddingTop: 6,
    paddingBottom: 12,
    borderBottomWidth: 1,
    borderBottomColor: colors.edge,
  },
  titleRow: { flex: 1, flexDirection: 'row', alignItems: 'center', gap: 10 },
  titleBadge: {
    width: 32,
    height: 32,
    borderRadius: 16,
    backgroundColor: colors.gold,
    alignItems: 'center',
    justifyContent: 'center',
  },
  title: { fontFamily: fonts.display, fontSize: 22, color: colors.sand },
  newChat: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    minHeight: 48,
    paddingHorizontal: 14,
    borderRadius: 24,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panelRaised,
  },
  newChatText: { color: colors.sand, fontSize: 14, fontFamily: fonts.semibold },
  close: {
    width: 56,
    height: 56,
    alignItems: 'center',
    justifyContent: 'center',
  },
  frame: { flex: 1, paddingBottom: 12 },
  list: { flexGrow: 1, paddingTop: 8, paddingBottom: 20, paddingHorizontal: 20, gap: 20 },
  frameBottom: { paddingHorizontal: 20 },
  empty: { marginTop: 28, flexDirection: 'column', gap: 12 },
  hint: { color: colors.dim, fontSize: 16, lineHeight: 24, maxWidth: 460 },
  chipWrap: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  citationChip: { flexGrow: 1 },
  followChip: { flexGrow: 1, minWidth: 76 },
  userRow: { flexDirection: 'row', justifyContent: 'flex-end' },
  userBubble: {
    maxWidth: '85%',
    borderRadius: sizes.radius,
    backgroundColor: colors.goldWash,
    borderBottomRightRadius: 4,
    paddingHorizontal: 16,
    paddingVertical: 12,
  },
  userText: { color: colors.sand, fontSize: sizes.body, lineHeight: 27 },
  retryButton: { marginTop: 6, alignSelf: 'flex-end', paddingVertical: 6, paddingLeft: 12 },
  retryText: { color: colors.lightAmber, fontSize: 15, fontFamily: fonts.semibold },
  retryTextDim: { color: colors.dim, fontSize: 15, fontFamily: fonts.medium },
  voiceFailBox: {
    marginTop: 10,
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panel,
    padding: 14,
  },
  voiceFailTitle: { color: colors.sand, fontSize: 16, fontFamily: fonts.semibold },
  voiceFailDetail: { color: colors.dim, fontSize: 14, marginTop: 4 },
  voiceFailRow: { flexDirection: 'row', gap: 4, marginTop: 8 },
  assistantRow: { flexDirection: 'row', justifyContent: 'flex-start' },
  assistantBubble: {
    width: '100%',
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panel,
    paddingHorizontal: 16,
    paddingVertical: 12,
    flexDirection: 'column',
    gap: 8,
    ...shadows.card,
  },
  assistantText: { color: colors.sand, fontSize: sizes.body, lineHeight: 27 },
  actionRow: { flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 4, borderTopWidth: 1, borderTopColor: colors.edge, paddingTop: 4 },
  actionText: { flex: 1, color: colors.dim, fontSize: 14, lineHeight: 21 },
  reviewLink: { color: colors.lightAmber, fontSize: 16, fontFamily: fonts.semibold },
  thinking: { color: colors.dim, fontSize: 16, paddingVertical: 4 },
  headerActions: { flexDirection: 'row', alignItems: 'center' },
  followCard: {
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panelRaised,
    padding: 14,
    flexDirection: 'column',
    gap: 10,
    ...shadows.card,
  },
  followTitle: { color: colors.sand, fontSize: 18, fontFamily: fonts.semibold },
  followGap: { flexDirection: 'column', gap: 8 },
  followQ: { color: colors.sand, fontSize: 16, fontFamily: fonts.medium },
  followInput: {
    minHeight: 52,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panelSoft,
    color: colors.sand,
    fontSize: 16,
    paddingHorizontal: 14,
  },
  chipsRow: { marginTop: 10 },
  chipRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  disclaimer: { color: colors.dim, fontSize: 12, lineHeight: 18, textAlign: 'center', marginTop: 8 },
  starterCard: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
    minHeight: 68,
    borderRadius: 20,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panel,
    padding: 14,
  },
  starterBadge: {
    width: 44,
    height: 44,
    borderRadius: 22,
    backgroundColor: colors.night,
    borderWidth: 1,
    borderColor: colors.edge,
    alignItems: 'center',
    justifyContent: 'center',
  },
  starterTexts: { flex: 1, minWidth: 0, flexDirection: 'column', gap: 2 },
  starterLabel: { color: colors.sand, fontSize: 17, fontFamily: fonts.semibold },
  starterSub: { color: colors.dim, fontSize: 14 },
});
