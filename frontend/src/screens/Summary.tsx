import { useCallback, useRef, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { printToFileAsync } from 'expo-print';
import { isAvailableAsync, shareAsync } from 'expo-sharing';
import { useFocusEffect } from '@react-navigation/native';
import type { CalendarDay, Summary as SummaryData } from '../api';
import { draftNarrative, getSummary } from '../api';
import { buildSummaryHtml, formatDuration, symptomLabel } from '../print';
import { colors, fonts, shadows, sizes } from '../theme';
import ScreenShell from '../components/ScreenShell';
import { ErrorText, GhostButton, PrimaryButton } from '../components/ui';

type WindowDays = 30 | 60 | 90;

function glyph(day: CalendarDay): string {
  const base = day.state === 'attack' ? '●' : day.state === 'no_attack' ? '○' : '';
  return day.unconfirmed && day.state !== 'not_logged' ? `${base}*` : base;
}

function describe(day: CalendarDay): string {
  const [, m, d] = day.day.split('-').map(Number);
  const label = `${m}/${d}`;
  const state =
    day.state === 'attack' ? 'attack day' : day.state === 'no_attack' ? 'no attack' : 'not logged';
  return `${label}: ${state}${day.unconfirmed ? ', unconfirmed' : ''}`;
}

function SummaryBody() {
  const [windowDays, setWindowDays] = useState<WindowDays>(30);
  const [summary, setSummary] = useState<SummaryData | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shared, setShared] = useState<string | null>(null);
  const [narrative, setNarrative] = useState<string | null>(null);
  const [drafting, setDrafting] = useState(false);
  const [narrativeError, setNarrativeError] = useState<string | null>(null);
  const loadedWindow = useRef<WindowDays | null>(null);

  const load = useCallback(async (days: WindowDays) => {
    const first = loadedWindow.current !== days;
    if (first) {
      setSummary(null);
      setShared(null);
      setNarrative(null);
      setNarrativeError(null);
      setFailed(false);
    }
    try {
      const fresh = await getSummary(days);
      setSummary(fresh);
      loadedWindow.current = days;
      setFailed(false);
    } catch {
      // Silent on refocus: keep showing the report already on screen.
      if (first) {
        setSummary(null);
        setFailed(true);
      }
    }
  }, []);

  useFocusEffect(
    useCallback(() => {
      load(windowDays);
    }, [load, windowDays]),
  );

  const draft = async () => {
    if (drafting) return;
    setDrafting(true);
    setNarrativeError(null);
    try {
      const result = await draftNarrative(windowDays);
      setNarrative(result.narrative);
    } catch (err) {
      setNarrative(null);
      setNarrativeError(err instanceof Error ? err.message : 'Could not draft. Try again.');
    } finally {
      setDrafting(false);
    }
  };

  const savePdf = async () => {
    if (!summary) return;
    setBusy(true);
    setError(null);
    setShared(null);
    try {
      const file = await printToFileAsync({ html: buildSummaryHtml(summary, narrative) });
      if (!(await isAvailableAsync())) {
        setError('Sharing is not available on this device, but the PDF was created.');
        return;
      }
      await shareAsync(file.uri);
      setShared(
        `PDF ready (${file.numberOfPages} ${file.numberOfPages === 1 ? 'page' : 'pages'}). Save or send it from the share sheet.`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create the PDF.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.column}>
      <View style={styles.windowRow}>
        {([30, 60, 90] as const).map((days) => {
          const selected = windowDays === days;
          return (
            <Pressable
              key={days}
              onPress={() => setWindowDays(days)}
              accessibilityRole="button"
              accessibilityLabel={`Last ${days} days`}
              accessibilityState={{ selected }}
              style={[styles.window, selected ? styles.windowSelected : null]}
            >
              <Text style={selected ? styles.windowTextSelected : styles.windowText}>{days} days</Text>
            </Pressable>
          );
        })}
      </View>

      <PrimaryButton
        title={busy ? 'Making PDF…' : 'Save / share PDF'}
        onPress={savePdf}
        disabled={busy || !summary}
      />
      {shared ? <Text style={styles.shared}>{shared}</Text> : null}
      {error ? <ErrorText>{error}</ErrorText> : null}

      {failed ? (
        <>
          <ErrorText>Could not load the summary. Is the server running?</ErrorText>
          <GhostButton title="Try again" onPress={() => load(windowDays)} />
        </>
      ) : null}

      {summary === null && !failed ? <Text style={styles.loading}>Loading the summary…</Text> : null}

      {summary ? (
        <>
          <Text style={styles.range}>
            {formatRange(summary.window_start, summary.window_end)}
          </Text>
          <Text style={styles.disclaimer}>
            Patient-recorded information. Not a medical record or a diagnosis.
          </Text>

          <View style={styles.card}>
            <Text style={styles.cardTitle}>Days</Text>
            <Text style={styles.body}>
              {summary.attack_days} with an attack entry · {summary.no_attack_days} “no attack”
              check-ins · {summary.not_logged_days} not logged
            </Text>
            <Text style={styles.dim}>
              {summary.attack_entries} attack {summary.attack_entries === 1 ? 'entry' : 'entries'}
              {summary.unconfirmed_entries > 0
                ? `, ${summary.unconfirmed_entries} unconfirmed`
                : ', all confirmed'}
            </Text>
          </View>

          <View style={styles.card}>
            <Text style={styles.cardTitle}>Day by day</Text>
            <View style={styles.grid} accessibilityRole="grid" accessibilityLabel="Day-by-day record">
              {['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((letter, i) => (
                <View key={`h-${i}`} style={styles.cell} accessible={false}>
                  <Text style={styles.headCell}>{letter}</Text>
                </View>
              ))}
              {summary.calendar.map((day) => (
                <View
                  key={day.day}
                  style={styles.cell}
                  accessible
                  accessibilityLabel={describe(day)}
                >
                  <Text style={day.state === 'not_logged' ? styles.dimCell : styles.cellDay}>
                    {Number(day.day.slice(8, 10))}
                  </Text>
                  <Text style={styles.cellMark}>{glyph(day)}</Text>
                </View>
              ))}
            </View>
            <Text style={styles.legend}>● attack day · ○ no attack · blank: not logged · * unconfirmed</Text>
          </View>

          <View style={styles.card}>
            <Text style={styles.cardTitle}>Duration and severity</Text>
            <Text style={styles.body}>
              {summary.duration.n === 0 || summary.duration.median_minutes === null
                ? 'No durations recorded.'
                : `Median ${formatDuration(summary.duration.median_minutes)} (range ${formatDuration(summary.duration.min_minutes ?? 0)}–${formatDuration(summary.duration.max_minutes ?? 0)}, ${summary.duration.n} entries).`}
            </Text>
            <Text style={styles.body}>
              Mild: {summary.severity_counts['1'] ?? 0} · Moderate: {summary.severity_counts['2'] ?? 0} · Severe:{' '}
              {summary.severity_counts['3'] ?? 0}
            </Text>
          </View>

          <View style={styles.card}>
            <Text style={styles.cardTitle}>Symptoms</Text>
            <SymptomLines summary={summary} />
          </View>

          <View style={styles.card}>
            <Text style={styles.cardTitle}>Acute medication</Text>
            {summary.medications.length === 0 ? (
              <Text style={styles.dim}>No medication recorded in this window.</Text>
            ) : (
              summary.medications.map((med) => (
                <View key={med.display_name} style={styles.medBlock}>
                  <Text style={styles.body}>{med.display_name}</Text>
                  <Text style={styles.dim}>
                    {med.attack_entries} {med.attack_entries === 1 ? 'attack' : 'attacks'} ·{' '}
                    {med.distinct_days} {med.distinct_days === 1 ? 'day' : 'days'}
                  </Text>
                  <Text style={styles.dim}>
                    {Object.entries(med.outcomes)
                      .map(([outcome, count]) => `${outcome.replace(/_/g, ' ')}: ${count}`)
                      .join('; ')}
                  </Text>
                </View>
              ))
            )}
          </View>

          <View style={styles.card}>
            <Text style={styles.cardTitle}>Reported triggers</Text>
            {Object.entries(summary.triggers).length === 0 ? (
              <Text style={styles.dim}>No triggers stated in this window.</Text>
            ) : (
              Object.entries(summary.triggers).map(([trigger, count]) => (
                <Text key={trigger} style={styles.body}>
                  {trigger} — {count} of {summary.triggers_n} attack entries
                </Text>
              ))
            )}
          </View>

          <View style={styles.card}>
            <Text style={styles.cardTitle}>Doctor summary</Text>
            {narrative ? (
              <Text style={styles.body}>{narrative}</Text>
            ) : (
              <Text style={styles.dim}>
                Draft a short paragraph from the numbers above to bring to an appointment.
              </Text>
            )}
            <GhostButton
              title={drafting ? 'Drafting…' : narrative ? 'Draft again' : 'Draft paragraph'}
              onPress={draft}
              disabled={drafting}
            />
            {narrativeError ? <ErrorText>{narrativeError}</ErrorText> : null}
            {narrative ? (
              <Text style={styles.dim}>Review before sharing. Included in the PDF below.</Text>
            ) : null}
          </View>

          <Text style={styles.footer}>
            Counts come from entries the user recorded. Days not logged are unknown, not attack-free.
          </Text>

          <View style={styles.card}>
            <Text style={styles.cardTitle}>Before you share</Text>
            <Text style={styles.body}>
              {windowDays}-day window · {formatRange(summary.window_start, summary.window_end)}
            </Text>
            <Text style={styles.dim}>
              {summary.attack_entries} attack {summary.attack_entries === 1 ? 'entry' : 'entries'} ·{' '}
              {summary.unconfirmed_entries > 0
                ? `${summary.unconfirmed_entries} unconfirmed (marked * in the PDF)`
                : 'all confirmed'}
            </Text>
            <Text style={styles.dim}>
              {narrative
                ? 'AI-drafted paragraph included — review it above before sharing.'
                : 'No AI-drafted paragraph included.'}
            </Text>
          </View>

          <PrimaryButton
            title={busy ? 'Making PDF…' : 'Save / share PDF'}
            onPress={savePdf}
            disabled={busy}
          />
          {shared ? <Text style={styles.shared}>{shared}</Text> : null}
          {error ? <ErrorText>{error}</ErrorText> : null}
        </>
      ) : null}
    </View>
  );
}

export default function Summary() {
  return (
    <ScreenShell>
      <SummaryBody />
    </ScreenShell>
  );
}

function formatRange(start: string, end: string): string {
  const fmt = (iso: string) => {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    });
  };
  return `${fmt(start)} – ${fmt(end)}`;
}

function SymptomLines({ summary }: { summary: SummaryData }) {
  const lines = Object.entries(summary.symptoms)
    .filter(([, count]) => count > 0)
    .map(([key, count]) => (
      <Text key={key} style={styles.body}>
        {symptomLabel(key)}: mentioned in {count} of {summary.symptoms_n} attack entries
      </Text>
    ));
  for (const [text, count] of Object.entries(summary.symptoms_other)) {
    lines.push(
      <Text key={`other-${text}`} style={styles.body}>
        {text}: mentioned in {count} of {summary.symptoms_n} attack entries
      </Text>,
    );
  }
  if (lines.length === 0) {
    return <Text style={styles.dim}>No symptoms mentioned in this window.</Text>;
  }
  return <>{lines}</>;
}

const styles = {
  column: { flexDirection: 'column', gap: 18, paddingTop: 24 } as const,
  loading: { color: colors.dim, fontSize: 20, textAlign: 'center' } as const,
  windowRow: { flexDirection: 'row', gap: 8 } as const,
  window: {
    flex: 1,
    minHeight: sizes.touch,
    borderRadius: sizes.radiusSmall,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panelRaised,
    alignItems: 'center',
    justifyContent: 'center',
  } as const,
  windowSelected: { borderColor: colors.lightAmber, backgroundColor: colors.lightAmber } as const,
  windowText: { color: colors.sand, fontSize: sizes.body } as const,
  windowTextSelected: { color: colors.ink, fontSize: sizes.body, fontFamily: fonts.semibold } as const,
  range: { color: colors.sand, fontSize: 24, fontFamily: fonts.display } as const,
  disclaimer: { color: colors.dim, fontSize: 16, marginTop: -8 } as const,
  card: {
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.edge,
    borderTopColor: colors.rise,
    backgroundColor: colors.panel,
    paddingHorizontal: 20,
    paddingVertical: 16,
    flexDirection: 'column',
    gap: 8,
    ...shadows.card,
  } as const,
  cardTitle: { color: colors.sand, fontSize: 20, fontFamily: fonts.semibold } as const,
  body: { color: colors.sand, fontSize: sizes.body, lineHeight: 27 } as const,
  dim: { color: colors.dim, fontSize: 16 } as const,
  grid: { flexDirection: 'row', flexWrap: 'wrap' } as const,
  cell: { width: '14.28%', alignItems: 'center', paddingVertical: 5 } as const,
  headCell: { color: colors.dim, fontSize: 14, fontFamily: fonts.semibold } as const,
  cellDay: { color: colors.sand, fontSize: 16 } as const,
  dimCell: { color: colors.dim, fontSize: 16, opacity: 0.55 } as const,
  cellMark: { color: colors.gold, fontSize: 14, minHeight: 18 } as const,
  legend: { color: colors.dim, fontSize: 14 } as const,
  medBlock: { flexDirection: 'column', gap: 2, marginTop: 6 } as const,
  footer: { color: colors.dim, fontSize: 15 } as const,
  shared: { color: colors.sand, fontSize: 16, textAlign: 'center' } as const,
};
