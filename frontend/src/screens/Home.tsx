import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Animated, AppState, StyleSheet, Text, View } from 'react-native';
import { ChevronLeft, ChevronRight, Mic, Timer } from 'lucide-react-native';
import Svg, { Circle } from 'react-native-svg';
import { useFocusEffect, useNavigation, type CompositeNavigationProp } from '@react-navigation/native';
import type { BottomTabNavigationProp } from '@react-navigation/bottom-tabs';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import type { Entry } from '../api';
import {
  deleteEntry,
  endAttack,
  formatDay,
  getApiBase,
  getHealth,
  listEntries,
  saveCheckin,
  startAttack,
} from '../api';
import { done, thump, tick, warn } from '../haptics';
import { getLogCoords } from '../location';
import { useChat } from '../chat';
import { useReducedMotionSetting } from '../motion';
import { sweepPendingVoice } from '../voice';
import { colors, fonts, shadows, sizes } from '../theme';
import ScreenShell from '../components/ScreenShell';
import { ErrorText, GhostButton, PrimaryButton, ScalePressable, StatusBadge } from '../components/ui';
import type { RootStackParamList, TabParamList } from '../nav';

function statusLabel(online: boolean | null, apiBase: string): string {
  if (online === null) return 'Checking server connection';
  return online ? `Connected to ${apiBase}` : `No server at ${apiBase}`;
}

interface Month {
  y: number;
  m: number;
}

function currentMonth(): Month {
  const now = new Date();
  return { y: now.getFullYear(), m: now.getMonth() };
}

function shiftMonth(month: Month, delta: number): Month {
  const date = new Date(month.y, month.m + delta, 1);
  return { y: date.getFullYear(), m: date.getMonth() };
}

function monthLabel(month: Month): string {
  return new Date(month.y, month.m, 1).toLocaleDateString(undefined, {
    month: 'long',
    year: 'numeric',
  });
}

function daysInMonth(month: Month): number {
  return new Date(month.y, month.m + 1, 0).getDate();
}

function monthBounds(month: Month): { start: string; end: string } {
  const mm = String(month.m + 1).padStart(2, '0');
  return {
    start: `${month.y}-${mm}-01`,
    end: `${month.y}-${mm}-${String(daysInMonth(month)).padStart(2, '0')}`,
  };
}

interface MonthData {
  attackDays: number;
  loggedDays: number;
  painAvg: number | null;
  painN: number;
  attackDates: Set<string>;
  checkinDates: Set<string>;
  /** Newest entry id per logged date (entries arrive newest-first). */
  dayEntryId: Map<string, string>;
}

function buildMonthData(entries: Entry[]): MonthData {
  const attackDates = new Set<string>();
  const checkinDates = new Set<string>();
  const dayEntryId = new Map<string, string>();
  let painSum = 0;
  let painN = 0;
  for (const entry of entries) {
    if (entry.entry_type === 'attack') attackDates.add(entry.entry_date);
    else checkinDates.add(entry.entry_date);
    if (!dayEntryId.has(entry.entry_date)) dayEntryId.set(entry.entry_date, entry.id);
    const pain = entry.attack?.pain_0_10;
    if (typeof pain === 'number') {
      painSum += pain;
      painN += 1;
    }
  }
  return {
    attackDays: attackDates.size,
    loggedDays: new Set([...attackDates, ...checkinDates]).size,
    painAvg: painN === 0 ? null : painSum / painN,
    painN,
    attackDates,
    checkinDates,
    dayEntryId,
  };
}

function formatAvg(avg: number): string {
  return String(Number(avg.toFixed(1)));
}

/**
 * Display kind for a record. "Migraine" appears only when the user wrote
 * the word themselves (transcript or notes) — never inferred.
 */
function entryKind(entry: Entry): 'Migraine' | 'Headache' | 'No-headache check-in' {
  if (entry.entry_type === 'no_attack') return 'No-headache check-in';
  const words = `${entry.transcript ?? ''} ${entry.attack?.notes ?? ''}`;
  return /\bmigraines?\b/i.test(words) ? 'Migraine' : 'Headache';
}

function elapsedParts(startIso: string, now: number): { h: number; m: number; s: number } {
  const total = Math.max(0, Math.floor((now - new Date(startIso).getTime()) / 1000));
  return { h: Math.floor(total / 3600), m: Math.floor((total % 3600) / 60), s: total % 60 };
}

function formatElapsed(startIso: string, now: number): string {
  const { h, m, s } = elapsedParts(startIso, now);
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m);
  const ss = String(s).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function describeElapsed(startIso: string, now: number): string {
  const { h, m } = elapsedParts(startIso, now);
  if (h === 0 && m === 0) return 'Attack running for less than a minute';
  const bits: string[] = [];
  if (h > 0) bits.push(`${h} hour${h === 1 ? '' : 's'}`);
  bits.push(`${m} minute${m === 1 ? '' : 's'}`);
  return `Attack running for ${bits.join(' ')}`;
}

function formatMinutes(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m === 0 ? `${h} hr` : `${h} hr ${m} min`;
}

function formatStartTime(startIso: string): string {
  return new Date(startIso).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}

const RING_SIZE = 216;
const RING_R = 100;
const RING_C = 2 * Math.PI * RING_R;

/** Elapsed ring: fills over each hour the attack runs. */
function TimerRing({ startIso, now }: { startIso: string; now: number }) {
  const fraction = ((now - new Date(startIso).getTime()) / 3600000) % 1;
  return (
    <Svg
      width={RING_SIZE}
      height={RING_SIZE}
      accessibilityElementsHidden
      importantForAccessibility="no-hide-descendants"
    >
      <Circle
        cx={RING_SIZE / 2}
        cy={RING_SIZE / 2}
        r={RING_R}
        stroke={colors.edge}
        strokeWidth={8}
        fill="none"
      />
      <Circle
        cx={RING_SIZE / 2}
        cy={RING_SIZE / 2}
        r={RING_R}
        stroke={colors.lightAmber}
        strokeWidth={8}
        fill="none"
        strokeLinecap="round"
        strokeDasharray={`${RING_C} ${RING_C}`}
        strokeDashoffset={RING_C * (1 - fraction)}
        transform={`rotate(-90 ${RING_SIZE / 2} ${RING_SIZE / 2})`}
      />
    </Svg>
  );
}

function LiveDot() {
  const reduceMotion = useReducedMotionSetting();
  const opacity = useRef(new Animated.Value(1)).current;
  useEffect(() => {
    if (reduceMotion) return;
    const pulse = Animated.loop(
      Animated.sequence([
        Animated.timing(opacity, { toValue: 0.3, duration: 1000, useNativeDriver: true }),
        Animated.timing(opacity, { toValue: 1, duration: 1000, useNativeDriver: true }),
      ]),
    );
    pulse.start();
    return () => pulse.stop();
  }, [opacity, reduceMotion]);
  return <Animated.View style={[styles.liveDot, { opacity }]} />;
}

function HomeBody() {
  const navigation =
    useNavigation<
      CompositeNavigationProp<
        BottomTabNavigationProp<TabParamList>,
        NativeStackNavigationProp<RootStackParamList>
      >
    >();
  const onLogAttack = () => navigation.navigate('Capture', {});
  const onAttachNote = (id: string) => navigation.navigate('Capture', { entryId: id });
  const onReview = () => navigation.navigate('History');
  const onOpenEntry = (id: string) => navigation.navigate('Detail', { id });
  const [today] = useState(currentMonth);
  const [month, setMonth] = useState<Month>(today);
  const [monthEntries, setMonthEntries] = useState<Entry[] | null>(null);
  const [monthFailed, setMonthFailed] = useState(false);
  const [monthError, setMonthError] = useState<string | null>(null);
  const [recent, setRecent] = useState<Entry[] | null>(null);
  const [recentFailed, setRecentFailed] = useState(false);
  const [recentError, setRecentError] = useState<string | null>(null);
  const [toReview, setToReview] = useState<number | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [waiting, setWaiting] = useState<number | null>(null);
  const [checkinState, setCheckinState] = useState<'idle' | 'saving' | 'done' | 'error'>('idle');
  const [online, setOnline] = useState<boolean | null>(null);
  const [apiBase] = useState(getApiBase);
  const [active, setActive] = useState<Entry | null | undefined>(undefined);
  const [justEnded, setJustEnded] = useState<{
    id: string;
    minutes: number;
    processing: boolean;
  } | null>(null);
  const delayedSweep = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [busy, setBusy] = useState<'starting' | 'ending' | 'discarding' | null>(null);
  const [timerError, setTimerError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [highlightId, setHighlightId] = useState<string | null>(null);
  const { justLoggedId, setJustLoggedId } = useChat();
  const pop = useRef(new Animated.Value(0)).current;
  const reduceMotion = useReducedMotionSetting();

  const statusLoaded = useRef(false);
  useFocusEffect(
    useCallback(() => {
      let cancelled = false;
      const first = !statusLoaded.current;
      getHealth().then((ok) => {
        if (!cancelled) setOnline(ok);
      });
      Promise.all([
        listEntries({ status: 'needs_processing', limit: 100 }),
        listEntries({ status: 'in_progress', entry_type: 'attack', limit: 1 }),
      ])
        .then(([pending, running]) => {
          if (!cancelled) {
            setWaiting(pending.count);
            setActive(running.entries[0] ?? null);
            statusLoaded.current = true;
          }
        })
        .catch(() => {
          if (!cancelled && first) {
            setWaiting(null);
            setActive(null);
          }
        });
      // Background voice recovery on every visit: picks up interrupted
      // captures and restarted-backend residue. Quiet by design.
      void sweepPendingVoice();
      return () => {
        cancelled = true;
      };
    }, [reloadKey]),
  );

  // Same recovery when the app returns from the background (no focus change).
  useEffect(() => {
    const subscription = AppState.addEventListener('change', (state) => {
      if (state === 'active') void sweepPendingVoice();
    });
    return () => subscription.remove();
  }, []);

  useEffect(
    () => () => {
      if (delayedSweep.current !== null) clearTimeout(delayedSweep.current);
    },
    [],
  );

  const loadedMonth = useRef<string | null>(null);
  useFocusEffect(
    useCallback(() => {
      let cancelled = false;
      const key = `${month.y}-${month.m}`;
      const first = loadedMonth.current !== key;
      if (first) {
        setMonthEntries(null);
        setMonthFailed(false);
        setMonthError(null);
      }
      const { start, end } = monthBounds(month);
      listEntries({ start, end, limit: 500 }).then(
        (body) => {
          if (!cancelled) {
            setMonthEntries(body.entries);
            setMonthFailed(false);
            setMonthError(null);
            loadedMonth.current = key;
          }
        },
        (err: unknown) => {
          if (!cancelled && first) {
            setMonthEntries(null);
            setMonthFailed(true);
            setMonthError(err instanceof Error ? err.message : 'Unknown error.');
          }
        },
      );
      return () => {
        cancelled = true;
      };
    }, [month, reloadKey]),
  );

  const recentLoaded = useRef(false);
  useFocusEffect(
    useCallback(() => {
      let cancelled = false;
      const first = !recentLoaded.current;
      if (first) {
        setRecent(null);
        setRecentFailed(false);
        setRecentError(null);
      }
      Promise.all([listEntries({ limit: 3 }), listEntries({ status: 'unconfirmed', limit: 100 })]).then(
        ([latest, review]) => {
          if (cancelled) return;
          setRecent(latest.entries);
          setRecentFailed(false);
          setRecentError(null);
          setToReview(review.count);
          recentLoaded.current = true;
        },
        (err: unknown) => {
          if (cancelled || !first) return;
          setRecent(null);
          setRecentFailed(true);
          setRecentError(err instanceof Error ? err.message : 'Unknown error.');
          setToReview(null);
        },
      );
      return () => {
        cancelled = true;
      };
    }, [reloadKey]),
  );

  useEffect(() => {
    if (active === null || active === undefined) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);

  // Landing half of the chat-log animation: the new card pops into Recent
  // activity when Home regains focus after a chat log.
  useFocusEffect(
    useCallback(() => {
      if (!justLoggedId) return;
      setHighlightId(justLoggedId);
      setJustLoggedId(null);
      if (!reduceMotion) {
        pop.setValue(0);
        Animated.spring(pop, { toValue: 1, useNativeDriver: true }).start();
      }
      const timer = setTimeout(() => setHighlightId(null), 4000);
      return () => clearTimeout(timer);
    }, [justLoggedId, reduceMotion, pop, setJustLoggedId]),
  );

  const monthData = useMemo(
    () => (monthEntries ? buildMonthData(monthEntries) : null),
    [monthEntries],
  );

  const moveMonth = (delta: -1 | 1) => {
    tick();
    setMonth((current) => shiftMonth(current, delta));
  };

  const refreshAll = () => setReloadKey((key) => key + 1);

  const checkin = async () => {
    setCheckinState('saving');
    try {
      await saveCheckin(await getLogCoords());
      thump();
      setCheckinState('done');
      refreshAll();
    } catch {
      setCheckinState('error');
    }
  };

  const start = async () => {
    setBusy('starting');
    setTimerError(null);
    try {
      const entry = await startAttack(await getLogCoords());
      thump();
      setActive(entry);
      setJustEnded(null);
      refreshAll();
    } catch {
      setTimerError('Could not start the timer. Check the server is running and try again.');
    } finally {
      setBusy(null);
    }
  };

  const end = async () => {
    if (!active) return;
    setBusy('ending');
    setTimerError(null);
    try {
      const entry = await endAttack(active.id, await getLogCoords());
      done();
      setActive(null);
      const stillWorking =
        entry.status === 'needs_processing' &&
        (!entry.transcript?.trim() ||
          entry.recordings.some(
            (r) =>
              r.status === 'pending' ||
              r.status === 'transcribing' ||
              (r.status === 'failed' && r.failure_kind === 'transient'),
          ));
      setJustEnded({
        id: entry.id,
        minutes: entry.attack?.duration_minutes ?? 0,
        processing: stillWorking,
      });
      if (entry.status === 'needs_processing') {
        setWaiting((w) => (w === null ? w : w + 1));
      }
      if (stillWorking) {
        // One delayed nudge in case the inline attempt hit a transient
        // failure; the backoff may still hold it for a later sweep.
        if (delayedSweep.current !== null) clearTimeout(delayedSweep.current);
        delayedSweep.current = setTimeout(() => {
          void sweepPendingVoice().then(() => refreshAll());
        }, 8000);
      }
      refreshAll();
    } catch {
      setTimerError('Could not end the timer. Check the server is running and try again.');
    } finally {
      setBusy(null);
    }
  };

  const discard = async () => {
    if (!active) return;
    setBusy('discarding');
    setTimerError(null);
    try {
      await deleteEntry(active.id);
      warn();
      setActive(null);
      refreshAll();
    } catch {
      setTimerError('Could not discard the timer. Check the server is running and try again.');
    } finally {
      setBusy(null);
    }
  };

  const askDiscard = () => {
    Alert.alert('Discard this timer?', 'The running timer will be deleted. This cannot be undone.', [
      { text: 'Keep timer', style: 'cancel' },
      { text: 'Discard', style: 'destructive', onPress: discard },
    ]);
  };

  const openDay = (dateStr: string) => {
    const id = monthData?.dayEntryId.get(dateStr);
    if (id) onOpenEntry(id);
  };

  const running = active !== null && active !== undefined;
  const timerStart = running ? (active.attack?.attack_start ?? active.created_at) : null;
  const isCurrent = month.y === today.y && month.m === today.m;
  const label = monthLabel(month);
  const daysIn = daysInMonth(month);
  const reviewCount = (toReview ?? 0) + (waiting ?? 0);
  const painValue = monthData ? (monthData.painAvg === null ? '—' : formatAvg(monthData.painAvg)) : '–';
  const painCaption = !monthData
    ? ''
    : monthData.painN === 0
      ? 'no scores'
      : `${monthData.painN} score${monthData.painN === 1 ? '' : 's'}`;
  const painLabel = !monthData
    ? 'Loading average pain'
    : monthData.painAvg === null
      ? `No pain scores recorded in ${label}`
      : `Average pain ${formatAvg(monthData.painAvg)} across ${monthData.painN} score${monthData.painN === 1 ? '' : 's'} in ${label}`;
  const blanks = (new Date(month.y, month.m, 1).getDay() + 6) % 7;
  const mm = String(month.m + 1).padStart(2, '0');
  const todayDay = isCurrent ? new Date().getDate() : null;

  const renderRecent = (entry: Entry) => {
    const kind = entryKind(entry);
    const pain = entry.attack?.pain_0_10;
    const status =
      entry.status === 'confirmed'
        ? 'Confirmed'
        : entry.status === 'in_progress'
          ? 'In progress'
          : 'Pending';
    const highlighted = highlightId === entry.id;
    return (
      <Animated.View
        key={entry.id}
        style={
          highlighted && !reduceMotion
            ? { opacity: pop, transform: [{ scale: pop }] }
            : undefined
        }
      >
        <ScalePressable
          onPress={() => onOpenEntry(entry.id)}
          accessibilityRole="button"
          accessibilityLabel={`${formatDay(entry.entry_date)}, ${kind}${typeof pain === 'number' ? `, pain ${pain} of 10` : ''}, ${status}. Open entry.`}
          style={[styles.recentCard, highlighted && styles.recentFresh]}
        >
          <View style={styles.recentLeft}>
            <Text style={styles.recentDate}>{formatDay(entry.entry_date)}</Text>
            <Text style={styles.recentSub}>
              {kind}
              {typeof pain === 'number' ? ` · Pain ${pain}/10` : ''}
            </Text>
          </View>
          <View style={styles.recentRight}>
            <StatusBadge tone={entry.status === 'confirmed' ? 'sage' : 'gold'}>{status}</StatusBadge>
            <ChevronRight size={20} color={colors.dim} />
          </View>
        </ScalePressable>
      </Animated.View>
    );
  };

  return (
    <View style={styles.column}>
      <View style={styles.monthRow}>
        <ScalePressable
          onPress={() => moveMonth(-1)}
          accessibilityRole="button"
          accessibilityLabel="Previous month"
          style={styles.chev}
        >
          <ChevronLeft size={24} color={colors.sand} />
        </ScalePressable>
        <View style={styles.monthHead}>
          <Text style={styles.monthTitle}>{label}</Text>
          <Text style={styles.monthSub}>Your month at a glance</Text>
        </View>
        <ScalePressable
          onPress={() => moveMonth(1)}
          disabled={isCurrent}
          accessibilityRole="button"
          accessibilityLabel="Next month"
          accessibilityState={{ disabled: isCurrent }}
          style={[styles.chev, isCurrent ? styles.chevDisabled : null]}
        >
          <ChevronRight size={24} color={isCurrent ? colors.edge : colors.sand} />
        </ScalePressable>
      </View>
      {!isCurrent ? (
        <ScalePressable
          onPress={() => {
            tick();
            setMonth(today);
          }}
          accessibilityRole="button"
          accessibilityLabel={`Back to ${monthLabel(today)}`}
          style={styles.todayJump}
        >
          <Text style={styles.todayJumpText}>Back to {monthLabel(today)}</Text>
        </ScalePressable>
      ) : null}

      <View style={styles.metrics}>
        <View
          style={styles.metric}
          accessible
          accessibilityLabel={
            monthData ? `Attack days in ${label}: ${monthData.attackDays}` : 'Loading attack days'
          }
        >
          <Text style={styles.metricLabel}>Attack days</Text>
          <Text style={styles.metricValue}>{monthData ? monthData.attackDays : '–'}</Text>
        </View>
        <View style={styles.metric} accessible accessibilityLabel={painLabel}>
          <Text style={styles.metricLabel}>Average pain</Text>
          <Text style={styles.metricValue}>{painValue}</Text>
          <Text style={styles.metricCaption}>{painCaption}</Text>
        </View>
        <View
          style={styles.metric}
          accessible
          accessibilityLabel={
            monthData
              ? `Logged days in ${label}: ${monthData.loggedDays} of ${daysIn}`
              : 'Loading logged days'
          }
        >
          <Text style={styles.metricLabel}>Logged days</Text>
          <Text style={styles.metricValue}>{monthData ? monthData.loggedDays : '–'}</Text>
          <Text style={styles.metricCaption}>{monthData ? `of ${daysIn}` : ''}</Text>
        </View>
      </View>
      {monthFailed ? (
        <>
          <ErrorText>Could not load this month. Is the server running?</ErrorText>
          {monthError ? <Text style={styles.errorDetail}>{monthError}</Text> : null}
          <GhostButton title="Try again" onPress={refreshAll} />
        </>
      ) : null}

      {running && timerStart ? (
        <>
          <View style={styles.timerCard}>
            <View style={styles.ringWrap}>
              <TimerRing startIso={timerStart} now={now} />
              <View style={styles.ringInner}>
                <View style={styles.liveRow}>
                  <LiveDot />
                  <Text style={styles.timerLabel}>Attack in progress</Text>
                </View>
                <Text style={styles.timerClock} accessibilityLabel={describeElapsed(timerStart, now)}>
                  {formatElapsed(timerStart, now)}
                </Text>
                <Text style={styles.timerStarted}>started {formatStartTime(timerStart)}</Text>
              </View>
            </View>
          </View>
          <ScalePressable
            onPress={end}
            disabled={busy !== null}
            accessibilityRole="button"
            accessibilityLabel="Attack ended"
            style={styles.logButton}
          >
            <Text style={styles.logText}>{busy === 'ending' ? 'Ending…' : 'Attack ended'}</Text>
          </ScalePressable>
          {timerError ? <ErrorText>{timerError}</ErrorText> : null}
          <View style={styles.actionRow}>
            <View style={styles.actionHalf}>
              <GhostButton title="Add note" onPress={() => onAttachNote(active.id)} />
            </View>
            <View style={styles.actionHalf}>
              <GhostButton
                title={busy === 'discarding' ? 'Discarding…' : 'Discard'}
                onPress={askDiscard}
                disabled={busy !== null}
              />
            </View>
          </View>
        </>
      ) : justEnded && !running ? (
        <>
          <View style={styles.doneBox}>
            <Text style={styles.doneText}>Attack ended · {formatMinutes(justEnded.minutes)}</Text>
          </View>
          {justEnded.processing ? (
            <Text style={styles.dimCenter}>Finishing transcription in the background…</Text>
          ) : null}
          <PrimaryButton title="Add details now" onPress={() => onAttachNote(justEnded.id)} />
          <GhostButton title="Later" onPress={() => setJustEnded(null)} />
        </>
      ) : (
        <>
          <View style={styles.actionCard}>
            <ScalePressable
              onPress={start}
              disabled={busy !== null}
              accessibilityRole="button"
              accessibilityLabel="Log a headache, start the attack timer"
              style={styles.actionBody}
            >
              <View style={styles.actionIcon}>
                <Timer size={26} color={colors.gold} />
              </View>
              <View style={styles.actionTexts}>
                <Text style={styles.actionTitle}>{busy === 'starting' ? 'Starting…' : 'Log a headache'}</Text>
                <Text style={styles.actionSub}>Starts the timer. Details can wait.</Text>
              </View>
            </ScalePressable>
            <ScalePressable
              onPress={onLogAttack}
              disabled={busy !== null}
              accessibilityRole="button"
              accessibilityLabel="Record a voice note instead"
              style={styles.micButton}
            >
              <View style={styles.micCircle}>
                <Mic size={24} color={colors.ink} />
              </View>
              <Text style={styles.micLabel}>Voice</Text>
            </ScalePressable>
          </View>
          {timerError ? <ErrorText>{timerError}</ErrorText> : null}
          <View style={styles.actionRow}>
            <View style={styles.actionHalf}>
              {checkinState === 'done' ? (
                <View style={styles.doneBox}>
                  <Text style={styles.doneText}>No attack today.</Text>
                </View>
              ) : (
                <GhostButton
                  title={checkinState === 'saving' ? 'Logging…' : 'No headache today'}
                  onPress={checkin}
                  disabled={checkinState === 'saving'}
                />
              )}
            </View>
            <View style={styles.actionHalf}>
              <GhostButton
                title={reviewCount > 0 ? `Review entries (${reviewCount})` : 'Review entries'}
                accessibilityLabel={
                  reviewCount > 0
                    ? `Review entries, ${reviewCount} need attention`
                    : 'Review entries'
                }
                onPress={onReview}
              />
            </View>
          </View>
          {checkinState === 'error' && (
            <ErrorText>Could not save. Check the server is running and try again.</ErrorText>
          )}
          <ScalePressable
            onPress={onLogAttack}
            accessibilityRole="button"
            accessibilityLabel="Log details for a past headache"
            style={styles.tertiary}
          >
            <Text style={styles.tertiaryText}>Log details for a past headache</Text>
          </ScalePressable>
        </>
      )}

      <Text style={styles.sectionTitle}>Recent activity</Text>
      {recent === null && !recentFailed ? (
        <Text style={styles.dimCenter}>Loading recent activity…</Text>
      ) : recentFailed || recent === null ? (
        <>
          <ErrorText>Could not load recent activity.</ErrorText>
          {recentError ? <Text style={styles.errorDetail}>{recentError}</Text> : null}
          <GhostButton title="Try again" onPress={refreshAll} />
        </>
      ) : recent.length === 0 ? (
        <Text style={styles.emptyState}>
          No entries yet — log your first headache above to start your record.
        </Text>
      ) : (
        <View style={styles.recentList}>{recent.map(renderRecent)}</View>
      )}

      <Text style={styles.sectionTitle}>Calendar</Text>
      <View style={styles.calGrid}>
        {['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((letter, i) => (
          <View key={`h-${i}`} style={styles.calCell} accessible={false}>
            <Text style={styles.calHead}>{letter}</Text>
          </View>
        ))}
        {Array.from({ length: blanks }, (_, i) => (
          <View key={`b-${i}`} style={styles.calCell} accessible={false} />
        ))}
        {Array.from({ length: daysIn }, (_, i) => {
          const day = i + 1;
          const dateStr = `${month.y}-${mm}-${String(day).padStart(2, '0')}`;
          const isAttack = monthData?.attackDates.has(dateStr) === true;
          const isClear = !isAttack && monthData?.checkinDates.has(dateStr) === true;
          const logged = isAttack || isClear;
          const isToday = todayDay === day;
          const content = (
            <>
              <Text style={logged ? styles.calDay : styles.calDayDim}>{day}</Text>
              <View
                style={[
                  styles.calDot,
                  isAttack ? styles.calDotAttack : isClear ? styles.calDotClear : styles.calDotEmpty,
                ]}
              />
            </>
          );
          return logged ? (
            <ScalePressable
              key={dateStr}
              onPress={() => openDay(dateStr)}
              accessibilityRole="button"
              accessibilityLabel={`${formatDay(dateStr)}${isToday ? ' (today)' : ''}: ${isAttack ? 'attack day' : 'no-headache day'}. Open records.`}
              style={[styles.calCell, isToday ? styles.calToday : null]}
            >
              {content}
            </ScalePressable>
          ) : (
            <View
              key={dateStr}
              style={[styles.calCell, isToday ? styles.calToday : null]}
              accessible
              accessibilityLabel={`${formatDay(dateStr)}${isToday ? ' (today)' : ''}: not logged`}
            >
              {content}
            </View>
          );
        })}
      </View>
      <View style={styles.legendRow}>
        <View style={styles.legendItem}>
          <View style={[styles.calDot, styles.calDotAttack]} />
          <Text style={styles.legendText}>attack</Text>
        </View>
        <View style={styles.legendItem}>
          <View style={[styles.calDot, styles.calDotClear]} />
          <Text style={styles.legendText}>no headache</Text>
        </View>
      </View>

      {online === false ? (
        <View style={styles.statusRow} accessible accessibilityLabel={statusLabel(online, apiBase)}>
          <View style={[styles.dot, styles.dotFail]} />
          <Text style={styles.statusText} numberOfLines={2}>
            {`No server at ${apiBase}. Entries you log may not save until it is back.`}
          </Text>
        </View>
      ) : null}
      {waiting !== null && waiting > 0 && (
        <Text style={styles.waiting}>
          {waiting} {waiting === 1 ? 'note is' : 'notes are'} still waiting to be understood.
        </Text>
      )}
    </View>
  );
}

export default function Home() {
  return (
    <ScreenShell>
      <HomeBody />
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  column: { flexDirection: 'column', gap: 18, paddingTop: 24 },
  monthRow: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  monthHead: { flex: 1, alignItems: 'center' },
  monthTitle: { color: colors.sand, fontSize: 24, fontFamily: fonts.display },
  monthSub: { color: colors.dim, fontSize: 15 },
  todayJump: {
    alignSelf: 'center',
    minHeight: 44,
    borderRadius: sizes.radiusPill,
    borderWidth: 1,
    borderColor: colors.lightAmber,
    paddingHorizontal: 16,
    alignItems: 'center',
    justifyContent: 'center',
  },
  todayJumpText: { color: colors.lightAmber, fontSize: 15, fontFamily: fonts.medium },
  chev: {
    width: 56,
    height: 56,
    borderRadius: 16,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panel,
    alignItems: 'center',
    justifyContent: 'center',
  },
  chevDisabled: { opacity: 0.5 },
  metrics: { flexDirection: 'row', gap: 10 },
  metric: {
    flex: 1,
    borderRadius: 16,
    borderWidth: 1,
    borderColor: colors.edge,
    borderTopColor: colors.rise,
    backgroundColor: colors.panel,
    paddingHorizontal: 8,
    paddingVertical: 12,
    alignItems: 'center',
    gap: 2,
    ...shadows.card,
  },
  metricLabel: { color: colors.dim, fontSize: 13, fontFamily: fonts.medium, textAlign: 'center' },
  metricValue: { color: colors.sand, fontSize: 28, fontFamily: fonts.display },
  metricCaption: { color: colors.dim, fontSize: 12, minHeight: 16 },
  logButton: {
    minHeight: 96,
    borderRadius: sizes.radius,
    backgroundColor: colors.gold,
    alignItems: 'center',
    justifyContent: 'center',
    ...shadows.card,
  },
  logText: { color: colors.ink, fontSize: 26, fontFamily: fonts.display },
  actionCard: {
    minHeight: 112,
    borderRadius: sizes.radius,
    backgroundColor: colors.gold,
    flexDirection: 'row',
    alignItems: 'center',
    gap: 16,
    paddingHorizontal: 20,
    paddingVertical: 16,
    ...shadows.card,
  },
  actionBody: { flex: 1, flexDirection: 'row', alignItems: 'center', gap: 16 },
  actionIcon: {
    width: 52,
    height: 52,
    borderRadius: 26,
    backgroundColor: colors.ink,
    alignItems: 'center',
    justifyContent: 'center',
  },
  micButton: { alignItems: 'center', justifyContent: 'center', gap: 4, minWidth: 64, minHeight: 64 },
  micCircle: {
    width: 52,
    height: 52,
    borderRadius: 26,
    borderWidth: 1,
    borderColor: colors.ink,
    alignItems: 'center',
    justifyContent: 'center',
  },
  micLabel: { color: colors.ink, fontSize: 12, fontFamily: fonts.semibold },
  actionTexts: { flex: 1, flexDirection: 'column', gap: 2 },
  actionTitle: { color: colors.ink, fontSize: 24, fontFamily: fonts.display },
  actionSub: { color: colors.ink, fontSize: 16, opacity: 0.75 },
  actionRow: { flexDirection: 'row', gap: 10 },
  actionHalf: { flex: 1 },
  tertiary: { minHeight: 56, alignItems: 'center', justifyContent: 'center' },
  tertiaryText: { color: colors.lightAmber, fontSize: 16, fontFamily: fonts.medium },
  sectionTitle: { color: colors.sand, fontSize: 20, fontFamily: fonts.semibold },
  recentList: { flexDirection: 'column', gap: 10 },
  recentCard: {
    minHeight: 72,
    borderRadius: 16,
    borderWidth: 1,
    borderColor: colors.edge,
    borderTopColor: colors.rise,
    backgroundColor: colors.panel,
    paddingHorizontal: 16,
    paddingVertical: 12,
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
    ...shadows.card,
  },
  recentFresh: { borderColor: colors.lightAmber, borderTopColor: colors.lightAmber },
  recentLeft: { flex: 1, flexDirection: 'column', gap: 2 },
  recentDate: { color: colors.sand, fontSize: 16, fontFamily: fonts.medium },
  recentSub: { color: colors.dim, fontSize: 14 },
  recentRight: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  dimCenter: { color: colors.dim, fontSize: 16, textAlign: 'center' },
  errorDetail: { color: colors.dim, fontSize: 14, textAlign: 'center' },
  emptyState: { color: colors.dim, fontSize: 16, textAlign: 'center', lineHeight: 24 },
  calGrid: { flexDirection: 'row', flexWrap: 'wrap' },
  calCell: {
    width: '14.28%',
    minHeight: 48,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 3,
    paddingVertical: 4,
  },
  calToday: { borderWidth: 1, borderColor: colors.lightAmber, borderRadius: 10 },
  calHead: { color: colors.dim, fontSize: 12, fontFamily: fonts.medium },
  calDay: { color: colors.sand, fontSize: 15, fontFamily: fonts.medium },
  calDayDim: { color: colors.dim, fontSize: 15, opacity: 0.55 },
  calDot: { width: 6, height: 6, borderRadius: 3 },
  calDotAttack: { backgroundColor: colors.gold },
  calDotClear: { backgroundColor: colors.sage },
  calDotEmpty: { backgroundColor: 'transparent' },
  legendRow: { flexDirection: 'row', justifyContent: 'center', gap: 16, marginTop: 4 },
  legendItem: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  legendText: { color: colors.dim, fontSize: 12 },
  timerCard: {
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.gold,
    backgroundColor: colors.panel,
    paddingVertical: 24,
    alignItems: 'center',
    ...shadows.card,
  },
  ringWrap: { alignItems: 'center', justifyContent: 'center' },
  ringInner: {
    position: 'absolute',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 2,
    paddingHorizontal: 28,
  },
  liveRow: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  liveDot: { width: 10, height: 10, borderRadius: 5, backgroundColor: colors.sage },
  timerLabel: { color: colors.dim, fontSize: 16, fontFamily: fonts.medium },
  timerClock: {
    color: colors.sand,
    fontSize: 48,
    fontFamily: fonts.semibold,
    fontVariant: ['tabular-nums'],
  },
  timerStarted: { color: colors.dim, fontSize: 15 },
  doneBox: {
    backgroundColor: colors.sage,
    borderRadius: sizes.radius,
    padding: 18,
    minHeight: sizes.touch,
    alignItems: 'center',
    justifyContent: 'center',
  },
  doneText: { color: colors.ink, fontSize: sizes.body, textAlign: 'center', fontFamily: fonts.semibold },
  waiting: { color: colors.dim, fontSize: 16, textAlign: 'center' },
  statusRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 8 },
  dot: { width: 10, height: 10, borderRadius: 5 },
  dotOk: { backgroundColor: colors.sage },
  dotFail: { backgroundColor: colors.clay },
  dotUnknown: { backgroundColor: colors.edge },
  statusText: { color: colors.dim, fontSize: 14, flexShrink: 1 },
});
