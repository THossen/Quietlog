import { useCallback, useMemo, useRef, useState } from 'react';
import {
  LayoutAnimation,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  UIManager,
  View,
} from 'react-native';
import { useFocusEffect, useNavigation, type CompositeNavigationProp } from '@react-navigation/native';
import type { BottomTabNavigationProp } from '@react-navigation/bottom-tabs';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import { ChevronDown, ChevronRight } from 'lucide-react-native';
import type { Entry } from '../api';
import { formatDay, listEntries, processEntry, summarize } from '../api';
import { tick } from '../haptics';
import { getLogCoords } from '../location';
import { useReducedMotionSetting } from '../motion';
import { colors, fonts, shadows, sizes } from '../theme';
import ScreenShell from '../components/ScreenShell';
import { ErrorText, GhostButton, StatusBadge } from '../components/ui';
import type { RootStackParamList, TabParamList } from '../nav';

type HistoryFilter = 'all' | 'review' | 'processing' | 'confirmed' | 'checkins';

const FILTERS: Array<{ id: HistoryFilter; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'review', label: 'To review' },
  { id: 'processing', label: 'Needs processing' },
  { id: 'confirmed', label: 'Confirmed' },
  { id: 'checkins', label: 'Check-ins' },
];

function matchesFilter(entry: Entry, filter: HistoryFilter): boolean {
  if (filter === 'all') return true;
  if (filter === 'checkins') return entry.entry_type === 'no_attack';
  if (filter === 'review') return entry.entry_type === 'attack' && entry.status === 'unconfirmed';
  if (filter === 'processing') return entry.entry_type === 'attack' && entry.status === 'needs_processing';
  return entry.status === 'confirmed';
}

function badgeTone(entry: Entry): 'gold' | 'clay' | 'sage' {
  if (entry.entry_type === 'no_attack') return 'sage';
  if (entry.status === 'needs_processing') return 'clay';
  if (entry.status === 'in_progress' || entry.status === 'unconfirmed') return 'gold';
  return 'sage';
}

function badgeLabel(entry: Entry): string {
  if (entry.entry_type === 'no_attack') return 'Check-in';
  if (entry.status === 'needs_processing') return 'Needs processing';
  if (entry.status === 'in_progress') return 'In progress';
  if (entry.status === 'unconfirmed') return 'To review';
  return 'Confirmed';
}

function monthKey(isoDate: string): string {
  return isoDate.slice(0, 7);
}

function monthTitle(key: string): string {
  const [year, month] = key.split('-').map(Number);
  return new Date(year, month - 1, 1).toLocaleDateString(undefined, {
    month: 'long',
    year: 'numeric',
  });
}

function currentMonthKey(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
}

function needsAttention(entry: Entry): boolean {
  return entry.status === 'unconfirmed' || entry.status === 'needs_processing';
}

if (
  Platform.OS === 'android' &&
  UIManager.setLayoutAnimationEnabledExperimental !== undefined
) {
  UIManager.setLayoutAnimationEnabledExperimental(true);
}

function ReviewListBody() {
  const navigation =
    useNavigation<
      CompositeNavigationProp<
        BottomTabNavigationProp<TabParamList>,
        NativeStackNavigationProp<RootStackParamList>
      >
    >();
  const onOpen = (id: string) => navigation.navigate('Detail', { id });
  const onAttachNote = (id: string) => navigation.navigate('Capture', { entryId: id });

  const [entries, setEntries] = useState<Entry[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [retrying, setRetrying] = useState<string | null>(null);
  const loadedOnce = useRef(false);

  const load = useCallback(async () => {
    try {
      const body = await listEntries({ limit: 500 });
      setEntries(body.entries);
      loadedOnce.current = true;
      setFailed(false);
    } catch {
      // Silent on refocus: keep showing the list already on screen.
      if (!loadedOnce.current) {
        setEntries(null);
        setFailed(true);
      }
    }
  }, []);

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load]),
  );

  const [openMonths, setOpenMonths] = useState<Set<string>>(() => new Set([currentMonthKey()]));
  const [filter, setFilter] = useState<HistoryFilter>('all');
  const reduceMotion = useReducedMotionSetting();

  const toggleMonth = (key: string) => {
    tick();
    if (!reduceMotion) LayoutAnimation.configureNext(LayoutAnimation.Presets.easeInEaseOut);
    setOpenMonths((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const filtered = useMemo(
    () => (entries ? entries.filter((entry) => matchesFilter(entry, filter)) : []),
    [entries, filter],
  );

  const months = useMemo(() => {
    if (!entries) return [];
    const buckets = new Map<string, Entry[]>();
    for (const entry of filtered) {
      const key = monthKey(entry.entry_date);
      const bucket = buckets.get(key);
      if (bucket) bucket.push(entry);
      else buckets.set(key, [entry]);
    }
    return [...buckets.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1));
  }, [entries, filtered]);

  const retry = async (id: string) => {
    setRetrying(id);
    try {
      await processEntry(id, await getLogCoords());
    } catch {
      /* error text comes from the reloaded entry */
    } finally {
      await load();
      setRetrying(null);
    }
  };

  if (failed) {
    return (
      <View style={styles.column}>
        <ErrorText>Could not load your notes. Is the server running?</ErrorText>
        <GhostButton title="Try again" onPress={load} />
      </View>
    );
  }

  if (entries === null) {
    return (
      <View style={styles.column}>
        <Text style={styles.loading}>Loading your notes…</Text>
      </View>
    );
  }

  if (entries.length === 0) {
    return (
      <View style={styles.column}>
        <Text style={styles.empty}>Nothing to review yet. Your next attack note will appear here.</Text>
      </View>
    );
  }

  const renderRow = (entry: Entry) => (
    <View key={entry.id} style={styles.card}>
      <Pressable
        onPress={() => onOpen(entry.id)}
        accessibilityRole="button"
        accessibilityLabel={`Review note from ${formatDay(entry.entry_date)}: ${summarize(entry)}`}
        style={styles.row}
      >
        <View style={styles.rowTop}>
          <Text style={styles.date}>{formatDay(entry.entry_date)}</Text>
          <StatusBadge tone={badgeTone(entry)}>{badgeLabel(entry)}</StatusBadge>
        </View>
        <Text style={styles.summary}>{summarize(entry)}</Text>
      </Pressable>
      {entry.entry_type === 'attack' && entry.status === 'needs_processing' && (
        <View style={styles.retryWrap}>
          {entry.transcript || entry.audio_path ? (
            <>
              {entry.processing_error ? <Text style={styles.dim}>{entry.processing_error}</Text> : null}
              <GhostButton
                title={retrying === entry.id ? 'Trying…' : 'Try again'}
                onPress={() => retry(entry.id)}
                disabled={retrying === entry.id}
              />
            </>
          ) : (
            <>
              <Text style={styles.dim}>Ended timer with no note yet.</Text>
              <GhostButton title="Add details" onPress={() => onAttachNote(entry.id)} />
            </>
          )}
        </View>
      )}
    </View>
  );

  return (
    <View style={styles.column}>
      <Text style={styles.heading}>History</Text>
      <View style={styles.chipRow}>
        {FILTERS.map((item) => {
          const selected = filter === item.id;
          const count = entries.filter((entry) => matchesFilter(entry, item.id)).length;
          return (
            <Pressable
              key={item.id}
              onPress={() => setFilter(item.id)}
              accessibilityRole="button"
              accessibilityLabel={`${item.label}, ${count} entries`}
              accessibilityState={{ selected }}
              style={[styles.chip, selected ? styles.chipSelected : null]}
            >
              <Text style={selected ? styles.chipTextSelected : styles.chipText}>
                {item.label} ({count})
              </Text>
            </Pressable>
          );
        })}
      </View>
      {months.length === 0 ? (
        <Text style={styles.empty}>Nothing matches this filter.</Text>
      ) : null}
      {months.map(([key, rows]) => {
        const open = openMonths.has(key);
        const attention = rows.filter(needsAttention).length;
        return (
          <View key={key}>
            <Pressable
              onPress={() => toggleMonth(key)}
              accessibilityRole="button"
              accessibilityState={{ expanded: open }}
              accessibilityLabel={`${monthTitle(key)}, ${rows.length} entries${attention > 0 ? `, ${attention} to review` : ''}`}
              style={styles.monthHeader}
            >
              <View style={styles.monthCopy}>
                <Text style={styles.monthTitle}>{monthTitle(key)}</Text>
                <Text style={styles.monthMeta}>
                  {rows.length} entr{rows.length === 1 ? 'y' : 'ies'}
                  {attention > 0 ? (
                    <Text style={styles.monthAttention}> · {attention} to review</Text>
                  ) : null}
                </Text>
              </View>
              {open ? (
                <ChevronDown size={22} color={colors.dim} />
              ) : (
                <ChevronRight size={22} color={colors.dim} />
              )}
            </Pressable>
            {open ? <View style={styles.sectionList}>{rows.map(renderRow)}</View> : null}
          </View>
        );
      })}
    </View>
  );
}

export default function ReviewList() {
  return (
    <ScreenShell>
      <ReviewListBody />
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  column: { flexDirection: 'column', gap: 20, paddingTop: 24 },
  heading: { color: colors.sand, fontSize: 26, fontFamily: fonts.display },
  chipRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  chip: {
    minHeight: 48,
    borderRadius: sizes.radiusPill,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panel,
    paddingHorizontal: 14,
    alignItems: 'center',
    justifyContent: 'center',
  },
  chipSelected: { borderColor: colors.lightAmber, backgroundColor: colors.lightAmber },
  chipText: { color: colors.sand, fontSize: 15, fontFamily: fonts.medium },
  chipTextSelected: { color: colors.ink, fontSize: 15, fontFamily: fonts.semibold },
  loading: { color: colors.dim, fontSize: 20, textAlign: 'center' },
  empty: { color: colors.sand, fontSize: 20, textAlign: 'center' },
  monthHeader: {
    minHeight: 64,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 12,
    paddingHorizontal: 20,
    paddingVertical: 12,
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panel,
    marginBottom: 10,
    ...shadows.card,
  },
  monthCopy: { flex: 1, flexDirection: 'column', gap: 2 },
  monthTitle: { color: colors.sand, fontSize: 20, fontFamily: fonts.semibold },
  monthMeta: { color: colors.dim, fontSize: 16 },
  monthAttention: { color: colors.lightAmber, fontFamily: fonts.medium },
  sectionList: { flexDirection: 'column', gap: 12 },
  card: {
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.edge,
    borderTopColor: colors.rise,
    backgroundColor: colors.panelRaised,
    ...shadows.card,
  },
  row: { minHeight: 64, paddingHorizontal: 20, paddingVertical: 16, flexDirection: 'column', gap: 8 },
  rowTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 12 },
  date: { color: colors.sand, fontSize: sizes.body, fontFamily: fonts.medium },
  summary: { color: colors.dim, fontSize: sizes.body },
  retryWrap: { paddingHorizontal: 20, paddingBottom: 16, flexDirection: 'column', gap: 12 },
  dim: { color: colors.dim, fontSize: 16 },
});
