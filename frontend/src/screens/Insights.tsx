import { useCallback, useEffect, useRef, useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { useFocusEffect } from '@react-navigation/native';
import type { Summary as SummaryData } from '../api';
import { getSummary } from '../api';
import { formatDuration, symptomLabel } from '../print';
import { colors, fonts, shadows, sizes } from '../theme';
import ScreenShell, { usePullRefresh } from '../components/ScreenShell';
import { ErrorText, GhostButton } from '../components/ui';

/** Attacks in trailing windows, derived from one 90-day summary. */
function countAttackDays(calendar: SummaryData['calendar'], fromEnd: number, days: number): number {
  const slice = calendar.slice(Math.max(0, calendar.length - fromEnd), calendar.length - fromEnd + days);
  return slice.filter((d) => d.state === 'attack').length;
}

function weekLabel(startIso: string, endIso: string): string {
  const short = (iso: string) => {
    const [, m, d] = iso.split('-').map(Number);
    return `${m}/${d}`;
  };
  return `${short(startIso)}–${short(endIso)}`;
}

function InsightsBody({ registerRefresh }: { registerRefresh: (fn: () => Promise<void>) => void }) {
  const [summary, setSummary] = useState<SummaryData | null>(null);
  const [failed, setFailed] = useState(false);
  const loadedOnce = useRef(false);

  const load = useCallback(async () => {
    try {
      setSummary(await getSummary(90));
      loadedOnce.current = true;
      setFailed(false);
    } catch {
      // Silent on refocus: keep showing the insights already on screen.
      if (!loadedOnce.current) {
        setSummary(null);
        setFailed(true);
      }
    }
  }, []);

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load]),
  );

  useEffect(() => {
    registerRefresh(load);
  }, [registerRefresh, load]);

  if (failed) {
    return (
      <View style={styles.column}>
        <ErrorText>Could not load insights. Is the server running?</ErrorText>
        <GhostButton title="Try again" onPress={load} />
      </View>
    );
  }

  if (summary === null) {
    return (
      <View style={styles.column}>
        <Text style={styles.loading}>Loading insights…</Text>
      </View>
    );
  }

  if (summary.attack_entries === 0) {
    return (
      <View style={styles.column}>
        <Text style={styles.heading}>Insights</Text>
        <Text style={styles.body}>Log attacks to see trends here. Nothing is shared or uploaded.</Text>
      </View>
    );
  }

  const last30 = countAttackDays(summary.calendar, 30, 30);
  const prior30 = countAttackDays(summary.calendar, 60, 30);
  const trend =
    last30 === prior30
      ? 'Same as the 30 days before that.'
      : last30 > prior30
        ? `More than the 30 days before that (${prior30}).`
        : `Fewer than the 30 days before that (${prior30}).`;

  const weeks: Array<{ label: string; count: number }> = [];
  for (let w = 0; w < 8; w += 1) {
    const slice = summary.calendar.slice(
      Math.max(0, summary.calendar.length - (w + 1) * 7),
      summary.calendar.length - w * 7,
    );
    if (slice.length === 0) break;
    weeks.unshift({
      label: weekLabel(slice[0].day, slice[slice.length - 1].day),
      count: slice.filter((d) => d.state === 'attack').length,
    });
  }
  const maxWeek = Math.max(1, ...weeks.map((w) => w.count));

  const topSymptoms = Object.entries(summary.symptoms)
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);
  const topTriggers = Object.entries(summary.triggers)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);
  const medRelief = summary.medications.map((med) => {
    const helped = (med.outcomes.relief ?? 0) + (med.outcomes.partial ?? 0);
    return { name: med.display_name, helped, taken: med.attack_entries };
  });

  return (
    <View style={styles.column}>
      <Text style={styles.heading}>Insights</Text>
      <Text style={styles.disclaimer}>
        Patterns in your entries over the last 90 days. Associations, not causes — and never
        medical advice.
      </Text>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Attack days</Text>
        <Text style={styles.big}>{last30}</Text>
        <Text style={styles.dim}>in the last 30 days. {trend}</Text>
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Attack days per week</Text>
        {weeks.map((week) => (
          <View
            key={week.label}
            style={styles.barRow}
            accessible
            accessibilityLabel={`Week of ${week.label}: ${week.count} attack days`}
          >
            <Text style={styles.barLabel}>{week.label}</Text>
            <View style={styles.track}>
              <View
                style={[
                  styles.fill,
                  { width: `${week.count === 0 ? 0 : Math.max(6, (week.count / maxWeek) * 100)}%` },
                ]}
              />
            </View>
            <Text style={styles.barCount}>{week.count}</Text>
          </View>
        ))}
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Most mentioned symptoms</Text>
        {topSymptoms.length === 0 ? (
          <Text style={styles.dim}>No symptoms mentioned yet.</Text>
        ) : (
          topSymptoms.map(([key, count]) => (
            <Text key={key} style={styles.body}>
              {symptomLabel(key)} — in {count} of {summary.symptoms_n} attack entries
            </Text>
          ))
        )}
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Most stated triggers</Text>
        {topTriggers.length === 0 ? (
          <Text style={styles.dim}>You have not stated any triggers.</Text>
        ) : (
          topTriggers.map(([trigger, count]) => (
            <Text key={trigger} style={styles.body}>
              {trigger} — {count} of {summary.triggers_n} attack entries
            </Text>
          ))
        )}
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Medication, as you reported it</Text>
        {medRelief.length === 0 ? (
          <Text style={styles.dim}>No medication recorded yet.</Text>
        ) : (
          medRelief.map((med) => (
            <Text key={med.name} style={styles.body}>
              {med.name}: relief reported {med.helped} of {med.taken} times taken
            </Text>
          ))
        )}
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Typical duration</Text>
        <Text style={styles.body}>
          {summary.duration.median_minutes === null
            ? 'No durations recorded yet.'
            : `Median ${formatDuration(summary.duration.median_minutes)} across ${summary.duration.n} entries.`}
        </Text>
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Pressure</Text>
        <Text style={styles.body}>{pressureLine(summary.weather)}</Text>
      </View>
    </View>
  );
}

function pressureLine(weather?: SummaryData['weather']): string {
  const tendency = weather?.tendency_n ?? 0;
  const falling = weather?.falling_n ?? 0;
  if (tendency === 0) {
    return 'No pressure readings yet. New logs record the weather automatically.';
  }
  return (
    `${falling} of ${tendency} logged attacks happened while pressure was falling.`
  );
}

export default function Insights() {
  const { refreshing, onRefresh, registerRefresh } = usePullRefresh();
  return (
    <ScreenShell refreshing={refreshing} onRefresh={onRefresh}>
      <InsightsBody registerRefresh={registerRefresh} />
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  column: { flexDirection: 'column', gap: 18, paddingTop: 24 },
  loading: { color: colors.dim, fontSize: 20, textAlign: 'center' },
  heading: { color: colors.sand, fontSize: 26, fontFamily: fonts.display },
  disclaimer: { color: colors.dim, fontSize: 16, marginTop: -8 },
  card: {
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.edge,
    borderTopColor: colors.rise,
    backgroundColor: colors.panelRaised,
    paddingHorizontal: 20,
    paddingVertical: 16,
    flexDirection: 'column',
    gap: 8,
    ...shadows.card,
  },
  cardTitle: { color: colors.sand, fontSize: 20, fontFamily: fonts.semibold },
  big: { color: colors.sand, fontSize: 46, fontFamily: fonts.display, lineHeight: 54 },
  body: { color: colors.sand, fontSize: sizes.body, lineHeight: 27 },
  dim: { color: colors.dim, fontSize: 16 },
  barRow: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  barLabel: { color: colors.dim, fontSize: 15, width: 92 },
  track: {
    flex: 1,
    height: 14,
    borderRadius: 7,
    backgroundColor: colors.night,
    overflow: 'hidden',
  },
  fill: { height: 14, borderRadius: 7, backgroundColor: colors.gold },
  barCount: { color: colors.sand, fontSize: 16, width: 24, textAlign: 'right' },
});
