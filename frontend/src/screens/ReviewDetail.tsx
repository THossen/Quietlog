import { useCallback, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Alert, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { useFocusEffect, useNavigation, useRoute, type RouteProp } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import type { AcuteMedication, AttackPatch, Entry, SymptomSet } from '../api';
import { SYMPTOM_LABELS, deleteEntry, formatDay, formatWeather, getEntry, patchEntry, processEntry } from '../api';
import { done } from '../haptics';
import { getLogCoords } from '../location';
import { colors, fonts, shadows, sizes } from '../theme';
import ScreenShell from '../components/ScreenShell';
import { ErrorText, GhostButton, PrimaryButton, StatusBadge } from '../components/ui';
import type { RootStackParamList } from '../nav';

type FixedSymptom = keyof Omit<SymptomSet, 'other'>;
const FIXED_SYMPTOMS = Object.keys(SYMPTOM_LABELS) as FixedSymptom[];

const OUTCOME_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: 'Not said' },
  { value: 'relief', label: 'Helped' },
  { value: 'partial', label: 'Helped a bit' },
  { value: 'none', label: 'No help' },
  { value: 'side_effects', label: 'Side effects' },
  { value: 'recurrence', label: 'Came back' },
  { value: 'unknown', label: 'Unknown' },
];

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function Highlighted({ text, quotes }: { text: string; quotes: string[] }) {
  const parts = useMemo<Array<{ key: number; text: string; mark: boolean }>>(() => {
    const usable = [...new Set(quotes.map((q) => q.trim()).filter((q) => q.length > 1))].sort(
      (a, b) => b.length - a.length,
    );
    if (usable.length === 0) return [{ key: 0, text, mark: false }];
    try {
      const re = new RegExp(`(${usable.map(escapeRegExp).join('|')})`, 'gi');
      return text.split(re).map((part, i) => ({ key: i, text: part, mark: i % 2 === 1 }));
    } catch {
      return [{ key: 0, text, mark: false }];
    }
  }, [text, quotes]);
  return (
    <Text style={styles.bodyText}>
      {parts.map((part) =>
        part.mark ? (
          <Text key={part.key} style={styles.mark}>
            {part.text}
          </Text>
        ) : (
          <Text key={part.key}>{part.text}</Text>
        ),
      )}
    </Text>
  );
}

function Field({ label, quote, children }: { label: string; quote?: string; children: ReactNode }) {
  return (
    <View style={styles.card}>
      <Text style={styles.fieldLabel}>{label}</Text>
      <View style={styles.fieldBody}>{children}</View>
      {quote ? <Text style={styles.quote}>“{quote}”</Text> : null}
    </View>
  );
}

function TriState({
  label,
  value,
  onChange,
}: {
  label: string;
  value: boolean | null;
  onChange: (value: boolean | null) => void;
}) {
  const options: Array<{ value: boolean | null; label: string }> = [
    { value: true, label: 'Yes' },
    { value: false, label: 'No' },
    { value: null, label: 'Not said' },
  ];
  return (
    <View>
      <Text style={styles.bodyText}>{label}</Text>
      <View style={styles.optionRow}>
        {options.map((option) => {
          const selected = value === option.value;
          return (
            <Pressable
              key={option.label}
              onPress={() => onChange(option.value)}
              accessibilityRole="button"
              accessibilityLabel={`${label}: ${option.label}`}
              accessibilityState={{ selected }}
              style={[styles.option, selected ? styles.optionSelected : null]}
            >
              <Text style={selected ? styles.optionTextSelected : styles.optionText}>
                {option.label}
              </Text>
            </Pressable>
          );
        })}
      </View>
    </View>
  );
}

interface Draft {
  time_expression: string;
  duration_minutes: number | null;
  ongoing: boolean | null;
  severity_function: number | null;
  pain_0_10: number | null;
  symptoms: SymptomSet;
  meds: AcuteMedication[];
  triggers: string[];
  menstrual_context: string;
  notes: string;
}

function toDraft(attack: NonNullable<Entry['attack']>): Draft {
  return {
    time_expression: attack.time_expression ?? '',
    duration_minutes: attack.duration_minutes,
    ongoing: attack.ongoing,
    severity_function: attack.severity_function,
    pain_0_10: attack.pain_0_10,
    symptoms: { ...attack.symptoms, other: [...attack.symptoms.other] },
    meds: attack.acute_medications.map((m) => ({ ...m })),
    triggers: [...attack.reported_triggers],
    menstrual_context: attack.menstrual_context ?? '',
    notes: attack.notes ?? '',
  };
}

const emptyToNull = (value: string): string | null => (value.trim() === '' ? null : value.trim());

function humanize(path: string): string {
  return path.replace(/[_[\].]/g, ' ').replace(/\s+/g, ' ').trim();
}

function formatStart(iso: string | null, precision: string): string {
  if (!iso) return 'Not stated';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const when = date.toLocaleString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
  if (precision === 'approximate') return `About ${when}`;
  if (precision === 'date_only') return `${when} (day only)`;
  return when;
}

function ReviewDetailBody() {
  const route = useRoute<RouteProp<RootStackParamList, 'Detail'>>();
  const navigation = useNavigation<NativeStackNavigationProp<RootStackParamList>>();
  const { id } = route.params;
  const onBack = () => navigation.goBack();
  const onAttachNote = (noteId: string) => navigation.navigate('Capture', { entryId: noteId });

  const [entry, setEntry] = useState<Entry | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [newTrigger, setNewTrigger] = useState('');
  const [newOther, setNewOther] = useState('');
  const [newMedName, setNewMedName] = useState('');

  const loadedOnce = useRef(false);
  const load = useCallback(async () => {
    try {
      const fresh = await getEntry(id);
      setEntry(fresh);
      // Never clobber an in-progress edit with a refocus reload.
      if (!editing) setDraft(fresh.attack ? toDraft(fresh.attack) : null);
      loadedOnce.current = true;
      setFailed(false);
    } catch {
      // Silent on refocus: keep showing the note already on screen.
      if (!loadedOnce.current) {
        setEntry(null);
        setFailed(true);
      }
    }
  }, [id, editing]);

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load]),
  );

  if (failed) {
    return (
      <View style={styles.column}>
        <ErrorText>Could not load this note.</ErrorText>
        <GhostButton title="Back to review" onPress={onBack} />
      </View>
    );
  }

  if (entry === null) {
    return (
      <View style={styles.column}>
        <Text style={styles.loading}>Loading…</Text>
      </View>
    );
  }

  const attack = entry.attack;
  const evidence = attack?.evidence ?? {};
  const quoteFor = (...paths: string[]): string | undefined =>
    paths.map((path) => evidence[path]).find((q) => q !== undefined);

  const retry = async () => {
    setBusy(true);
    setError(null);
    try {
      await processEntry(entry.id, await getLogCoords());
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Retry failed.');
    } finally {
      setBusy(false);
    }
  };

  const saveEdits = async () => {
    if (!draft) return;
    setBusy(true);
    setError(null);
    const patch: AttackPatch = {
      time_expression: emptyToNull(draft.time_expression),
      duration_minutes: draft.duration_minutes,
      ongoing: draft.ongoing,
      severity_function: draft.severity_function,
      pain_0_10: draft.pain_0_10,
      symptoms: { ...draft.symptoms },
      acute_medications: draft.meds.filter((m) => m.name.trim() !== ''),
      reported_triggers: draft.triggers,
      menstrual_context: emptyToNull(draft.menstrual_context),
      notes: emptyToNull(draft.notes),
    };
    try {
      const updated = await patchEntry(entry.id, { attack: patch });
      setEntry(updated);
      setDraft(updated.attack ? toDraft(updated.attack) : null);
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save edits.');
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      const updated = await patchEntry(entry.id, { confirm: true });
      done();
      setEntry(updated);
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not confirm.');
    } finally {
      setBusy(false);
    }
  };

  const cancelEdit = () => {
    setDraft(attack ? toDraft(attack) : null);
    setEditing(false);
    setNewTrigger('');
    setNewOther('');
    setNewMedName('');
  };

  const discard = async () => {
    setBusy(true);
    setError(null);
    try {
      await deleteEntry(entry.id);
      onBack();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not discard.');
    } finally {
      setBusy(false);
    }
  };

  const askDiscard = () => {
    Alert.alert('Delete this entry?', 'This cannot be undone.', [
      { text: 'Keep', style: 'cancel' },
      { text: 'Delete', style: 'destructive', onPress: discard },
    ]);
  };

  if (entry.entry_type === 'no_attack' || attack === null) {
    return (
      <View style={styles.column}>
        <Text style={styles.center}>No attack logged for {formatDay(entry.entry_date)}.</Text>
        <GhostButton title="Back to review" onPress={onBack} />
      </View>
    );
  }

  if (entry.status === 'needs_processing') {
    const hasContent = Boolean(entry.transcript || entry.audio_path);
    return (
      <View style={styles.column}>
        <Text style={styles.heading}>{formatDay(entry.entry_date)}</Text>
        {entry.transcript ? (
          <View style={styles.card}>
            <Text style={styles.bodyText}>{entry.transcript}</Text>
          </View>
        ) : entry.audio_path ? (
          <Text style={styles.dim}>Voice note saved, transcript pending.</Text>
        ) : (
          <Text style={styles.dim}>No details yet — this timer ended with no note.</Text>
        )}
        {entry.processing_error && hasContent ? (
          <Text style={styles.dim}>{entry.processing_error}</Text>
        ) : null}
        {error ? <ErrorText>{error}</ErrorText> : null}
        {hasContent ? (
          <>
            <PrimaryButton title={busy ? 'Trying…' : 'Try again'} onPress={retry} disabled={busy} />
            <GhostButton
              title={busy ? 'Deleting…' : 'Delete this entry'}
              onPress={askDiscard}
              disabled={busy}
            />
          </>
        ) : (
          <>
            <PrimaryButton title="Add details" onPress={() => onAttachNote(entry.id)} />
            <GhostButton
              title={busy ? 'Deleting…' : 'Delete this entry'}
              onPress={askDiscard}
              disabled={busy}
            />
          </>
        )}
        <GhostButton title="Back to review" onPress={onBack} />
      </View>
    );
  }

  const presentSymptoms = FIXED_SYMPTOMS.filter((name) => attack.symptoms[name] === true);
  const deniedSymptoms = FIXED_SYMPTOMS.filter((name) => attack.symptoms[name] === false);

  const setMed = (index: number, update: Partial<AcuteMedication>) => {
    if (!draft) return;
    setDraft({ ...draft, meds: draft.meds.map((m, i) => (i === index ? { ...m, ...update } : m)) });
  };

  return (
    <View style={styles.column}>
      <View style={styles.titleRow}>
        <Text style={styles.heading}>{formatDay(entry.entry_date)}</Text>
        {entry.status === 'confirmed' ? (
          <StatusBadge tone="sage">Confirmed</StatusBadge>
        ) : entry.status === 'in_progress' ? (
          <StatusBadge tone="gold">In progress</StatusBadge>
        ) : (
          <StatusBadge tone="gold">Not confirmed yet</StatusBadge>
        )}
      </View>
      {formatWeather(entry.weather) ? (
        <Text style={styles.weather}>
          {formatWeather(entry.weather)}
          {entry.weather?.basis === 'attack_time' ? ' · at attack time' : ''}
        </Text>
      ) : null}

      {entry.status === 'in_progress' && !editing ? (
        <View style={[styles.card, styles.infoCard]}>
          <Text style={styles.infoTitle}>Timer running</Text>
          <Text style={styles.bodyText}>
            End the timer on Home to stamp the final duration, then add details.
          </Text>
        </View>
      ) : null}

      {entry.transcript ? (
        <View style={styles.card}>
          <Text style={styles.fieldLabel}>Your words</Text>
          <Highlighted text={entry.transcript} quotes={Object.values(evidence)} />
        </View>
      ) : null}

      {attack.unsupported_fields.length > 0 && !editing ? (
        <View style={[styles.card, styles.warnCard]}>
          <Text style={styles.warnTitle}>Not supported by what you said</Text>
          {attack.unsupported_fields.map((path) => (
            <Text key={path} style={styles.bodyText}>
              • {humanize(path)}
            </Text>
          ))}
        </View>
      ) : null}

      {!editing ? (
        <View style={styles.fields}>
          <Field label="Started" quote={quoteFor('time_expression')}>
            <Text style={styles.bodyText}>
              {formatStart(attack.attack_start, attack.attack_start_precision)}
            </Text>
          </Field>
          <Field label="How long" quote={quoteFor('duration_minutes', 'ongoing')}>
            <Text style={styles.bodyText}>
              {attack.duration_minutes !== null ? `${attack.duration_minutes} minutes` : 'Not stated'}
              {attack.ongoing === true ? ' · still going' : ''}
              {attack.ongoing === false ? ' · over now' : ''}
            </Text>
          </Field>
          <Field label="Severity" quote={quoteFor('severity_function', 'pain_0_10')}>
            <Text style={styles.bodyText}>
              {attack.severity_function === 1 ? 'Mild — could function' : ''}
              {attack.severity_function === 2 ? 'Moderate — activities slowed' : ''}
              {attack.severity_function === 3 ? 'Severe — could not do activities' : ''}
              {attack.severity_function === null ? 'Not stated' : ''}
              {attack.pain_0_10 !== null ? ` · pain ${attack.pain_0_10}/10` : ''}
            </Text>
          </Field>
          <Field
            label="Symptoms"
            quote={quoteFor(
              'symptoms.nausea',
              'symptoms.vomiting',
              'symptoms.aura',
              'symptoms.light_sensitivity',
              'symptoms.sound_sensitivity',
              'symptoms.smell_sensitivity',
              'symptoms.dizziness',
            )}
          >
            <Text style={styles.bodyText}>
              {presentSymptoms.length === 0 &&
              deniedSymptoms.length === 0 &&
              attack.symptoms.other.length === 0
                ? 'Not stated'
                : [
                    presentSymptoms.map((s) => SYMPTOM_LABELS[s]).join(', '),
                    deniedSymptoms.length > 0
                      ? `no ${deniedSymptoms.map((s) => SYMPTOM_LABELS[s].toLowerCase()).join(', no ')}`
                      : '',
                    attack.symptoms.other.join('; '),
                  ]
                    .filter(Boolean)
                    .join(' · ')}
            </Text>
          </Field>
          <Field label="Medication" quote={quoteFor('acute_medications[0].name', 'acute_medications[0].outcome')}>
            {attack.acute_medications.length === 0 ? (
              <Text style={styles.bodyText}>None taken</Text>
            ) : (
              attack.acute_medications.map((med, i) => (
                <Text key={`${med.name}-${i}`} style={styles.bodyText}>
                  {med.name}
                  {med.dose ? ` ${med.dose}` : ''}
                  {med.time_taken ? `, ${med.time_taken}` : ''}
                  {med.outcome
                    ? ` — ${OUTCOME_OPTIONS.find((o) => o.value === med.outcome)?.label ?? med.outcome}`
                    : ''}
                  {med.second_dose ? ' · second dose' : ''}
                </Text>
              ))
            )}
          </Field>
          <Field label="Triggers as you stated them" quote={quoteFor('reported_triggers[0]')}>
            <Text style={styles.bodyText}>
              {attack.reported_triggers.length === 0 ? 'None stated' : attack.reported_triggers.join('; ')}
            </Text>
          </Field>
          {attack.menstrual_context || attack.notes ? (
            <Field label="More context" quote={quoteFor('menstrual_context', 'notes')}>
              <Text style={styles.bodyText}>
                {[attack.menstrual_context, attack.notes].filter(Boolean).join(' · ')}
              </Text>
            </Field>
          ) : null}
        </View>
      ) : draft ? (
        <View style={styles.editBox}>
          <Text style={styles.heading}>Edit this note</Text>
          <View>
            <Text style={styles.bodyText}>Started, as you said it</Text>
            <TextInput
              value={draft.time_expression}
              onChangeText={(time_expression) => setDraft({ ...draft, time_expression })}
              placeholder="since 2pm"
              placeholderTextColor={colors.dim}
              style={styles.input}
            />
          </View>
          <View>
            <Text style={styles.bodyText}>How long (minutes)</Text>
            <TextInput
              value={draft.duration_minutes === null ? '' : String(draft.duration_minutes)}
              onChangeText={(text) => {
                if (text.trim() === '') {
                  setDraft({ ...draft, duration_minutes: null });
                  return;
                }
                const n = Math.max(1, Math.floor(Number(text)));
                setDraft({ ...draft, duration_minutes: Number.isNaN(n) ? null : n });
              }}
              keyboardType="number-pad"
              placeholder="Not stated"
              placeholderTextColor={colors.dim}
              style={styles.input}
            />
          </View>
          <TriState
            label="Still going"
            value={draft.ongoing}
            onChange={(ongoing) => setDraft({ ...draft, ongoing })}
          />
          <View>
            <Text style={styles.bodyText}>Severity</Text>
            <View style={styles.optionRow}>
              {[
                { value: 1 as const, label: 'Mild' },
                { value: 2 as const, label: 'Moderate' },
                { value: 3 as const, label: 'Severe' },
                { value: null, label: 'Clear' },
              ].map((option) => {
                const selected = draft.severity_function === option.value;
                return (
                  <Pressable
                    key={option.label}
                    onPress={() => setDraft({ ...draft, severity_function: option.value })}
                    accessibilityRole="button"
                    accessibilityLabel={`Severity ${option.label}`}
                    accessibilityState={{ selected }}
                    style={[styles.option, styles.optionSmall, selected ? styles.optionSelected : null]}
                  >
                    <Text style={selected ? styles.optionTextSelected : styles.optionText}>
                      {option.label}
                    </Text>
                  </Pressable>
                );
              })}
            </View>
          </View>
          <View>
            <Text style={styles.bodyText}>Pain 0–10 (only if you stated a number)</Text>
            <TextInput
              value={draft.pain_0_10 === null ? '' : String(draft.pain_0_10)}
              onChangeText={(text) => {
                if (text.trim() === '') {
                  setDraft({ ...draft, pain_0_10: null });
                  return;
                }
                const n = Math.max(0, Math.min(10, Math.floor(Number(text))));
                setDraft({ ...draft, pain_0_10: Number.isNaN(n) ? null : n });
              }}
              keyboardType="number-pad"
              placeholder="Not stated"
              placeholderTextColor={colors.dim}
              style={styles.input}
            />
          </View>
          {FIXED_SYMPTOMS.map((name) => (
            <TriState
              key={name}
              label={SYMPTOM_LABELS[name]}
              value={draft.symptoms[name]}
              onChange={(value) =>
                setDraft({ ...draft, symptoms: { ...draft.symptoms, [name]: value } })
              }
            />
          ))}
          <OtherList
            label="Other symptoms"
            items={draft.symptoms.other}
            draft={newOther}
            setDraft={setNewOther}
            onRemove={(index) =>
              setDraft({
                ...draft,
                symptoms: { ...draft.symptoms, other: draft.symptoms.other.filter((_, i) => i !== index) },
              })
            }
            onAdd={() => {
              if (!newOther.trim()) return;
              setDraft({
                ...draft,
                symptoms: { ...draft.symptoms, other: [...draft.symptoms.other, newOther.trim()] },
              });
              setNewOther('');
            }}
          />
          <View style={styles.medSection}>
            <Text style={styles.bodyText}>Medication</Text>
            {draft.meds.map((med, index) => (
              <View key={`${med.name}-${index}`} style={styles.medCard}>
                <Text style={styles.bodyText}>Name</Text>
                <TextInput
                  value={med.name}
                  onChangeText={(name) => setMed(index, { name })}
                  style={styles.input}
                />
                <Text style={styles.bodyText}>Dose</Text>
                <TextInput
                  value={med.dose ?? ''}
                  onChangeText={(dose) => setMed(index, { dose: dose === '' ? null : dose })}
                  style={styles.input}
                />
                <Text style={styles.bodyText}>Taken</Text>
                <TextInput
                  value={med.time_taken ?? ''}
                  onChangeText={(time_taken) => setMed(index, { time_taken: time_taken === '' ? null : time_taken })}
                  style={styles.input}
                />
                <Text style={styles.bodyText}>Outcome</Text>
                <View style={styles.outcomeList}>
                  {OUTCOME_OPTIONS.map((option) => {
                    const selected = (med.outcome ?? '') === option.value;
                    return (
                      <Pressable
                        key={option.value}
                        onPress={() => setMed(index, { outcome: option.value === '' ? null : option.value })}
                        accessibilityRole="button"
                        accessibilityLabel={`Outcome ${option.label}`}
                        accessibilityState={{ selected }}
                        style={[styles.option, selected ? styles.optionSelected : null]}
                      >
                        <Text style={selected ? styles.optionTextSelected : styles.optionText}>
                          {option.label}
                        </Text>
                      </Pressable>
                    );
                  })}
                </View>
                <Pressable
                  onPress={() => setMed(index, { second_dose: med.second_dose === true ? null : true })}
                  accessibilityRole="checkbox"
                  accessibilityLabel="Second dose"
                  accessibilityState={{ checked: med.second_dose === true }}
                  style={styles.checkRow}
                >
                  <View style={[styles.checkbox, med.second_dose === true ? styles.checkboxOn : null]}>
                    {med.second_dose === true ? <Text style={styles.checkMark}>✓</Text> : null}
                  </View>
                  <Text style={styles.bodyText}>Second dose</Text>
                </Pressable>
                <GhostButton
                  title={`Remove ${med.name || 'medication'}`}
                  onPress={() => setDraft({ ...draft, meds: draft.meds.filter((_, i) => i !== index) })}
                />
              </View>
            ))}
            <View style={styles.addRow}>
              <TextInput
                value={newMedName}
                onChangeText={setNewMedName}
                placeholder="Add medication by name"
                placeholderTextColor={colors.dim}
                aria-label="Add medication by name"
                style={[styles.input, styles.addInput]}
              />
              <Pressable
                onPress={() => {
                  if (!newMedName.trim()) return;
                  setDraft({
                    ...draft,
                    meds: [
                      ...draft.meds,
                      { name: newMedName.trim(), dose: null, time_taken: null, outcome: null, second_dose: null },
                    ],
                  });
                  setNewMedName('');
                }}
                accessibilityRole="button"
                accessibilityLabel="Add medication"
                style={styles.addButton}
              >
                <Text style={styles.optionText}>Add</Text>
              </Pressable>
            </View>
          </View>
          <OtherList
            label="Triggers, as you stated them"
            items={draft.triggers}
            draft={newTrigger}
            setDraft={setNewTrigger}
            onRemove={(index) =>
              setDraft({ ...draft, triggers: draft.triggers.filter((_, i) => i !== index) })
            }
            onAdd={() => {
              if (!newTrigger.trim()) return;
              setDraft({ ...draft, triggers: [...draft.triggers, newTrigger.trim()] });
              setNewTrigger('');
            }}
          />
          <View>
            <Text style={styles.bodyText}>Menstrual context (only if stated)</Text>
            <TextInput
              value={draft.menstrual_context}
              onChangeText={(menstrual_context) => setDraft({ ...draft, menstrual_context })}
              placeholder="Not stated"
              placeholderTextColor={colors.dim}
              style={styles.input}
            />
          </View>
          <View>
            <Text style={styles.bodyText}>Notes</Text>
            <TextInput
              value={draft.notes}
              onChangeText={(notes) => setDraft({ ...draft, notes })}
              multiline
              numberOfLines={3}
              textAlignVertical="top"
              style={[styles.input, styles.notesInput]}
            />
          </View>
          <PrimaryButton title={busy ? 'Saving…' : 'Save changes'} onPress={saveEdits} disabled={busy} />
          <GhostButton title="Cancel" onPress={cancelEdit} />
        </View>
      ) : null}

      {error ? <ErrorText>{error}</ErrorText> : null}

      {!editing && entry.status === 'unconfirmed' ? (
        <PrimaryButton
          title={busy ? 'Confirming…' : 'Confirm — this is right'}
          onPress={confirm}
          disabled={busy}
        />
      ) : null}
      {!editing ? <GhostButton title="Edit" onPress={() => setEditing(true)} /> : null}
      <GhostButton title="Back to review" onPress={onBack} />
      {!editing ? (
        <GhostButton
          title={busy ? 'Deleting…' : 'Delete this entry'}
          onPress={askDiscard}
          disabled={busy}
        />
      ) : null}
    </View>
  );
}

export default function ReviewDetail() {
  return (
    <ScreenShell topPad>
      <ReviewDetailBody />
    </ScreenShell>
  );
}

function OtherList({
  label,
  items,
  draft,
  setDraft,
  onRemove,
  onAdd,
}: {
  label: string;
  items: string[];
  draft: string;
  setDraft: (value: string) => void;
  onRemove: (index: number) => void;
  onAdd: () => void;
}) {
  return (
    <View>
      <Text style={styles.bodyText}>{label}</Text>
      {items.map((item, index) => (
        <View key={`${item}-${index}`} style={styles.chipRow}>
          <Text style={styles.chipText}>{item}</Text>
          <Pressable
            onPress={() => onRemove(index)}
            accessibilityRole="button"
            accessibilityLabel={`Remove ${item}`}
            style={styles.chipRemove}
          >
            <Text style={styles.dim}>✕</Text>
          </Pressable>
        </View>
      ))}
      <View style={styles.addRow}>
        <TextInput
          value={draft}
          onChangeText={setDraft}
          placeholder="Add another"
          placeholderTextColor={colors.dim}
          aria-label={`Add to ${label}`}
          style={[styles.input, styles.addInput]}
        />
        <Pressable
          onPress={onAdd}
          accessibilityRole="button"
          accessibilityLabel={`Add to ${label}`}
          style={styles.addButton}
        >
          <Text style={styles.optionText}>Add</Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  column: { flexDirection: 'column', gap: 18, paddingTop: 24 },
  loading: { color: colors.dim, fontSize: 20, textAlign: 'center' },
  center: { color: colors.sand, fontSize: 20, textAlign: 'center' },
  heading: { color: colors.sand, fontSize: 26, fontFamily: fonts.display },
  titleRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 12 },
  card: {
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.edge,
    borderTopColor: colors.rise,
    backgroundColor: colors.panelRaised,
    paddingHorizontal: 20,
    paddingVertical: 16,
    ...shadows.card,
  },
  warnCard: { borderColor: colors.clay },
  warnTitle: { color: colors.clay, fontSize: sizes.body, fontFamily: fonts.medium, marginBottom: 4 },
  infoCard: { borderColor: colors.gold },
  infoTitle: { color: colors.sand, fontSize: sizes.body, fontFamily: fonts.semibold, marginBottom: 4 },
  fieldLabel: { color: colors.dim, fontSize: 16, fontFamily: fonts.medium },
  fieldBody: { marginTop: 4 },
  bodyText: { color: colors.sand, fontSize: 20, lineHeight: 28 },
  quote: { color: colors.dim, fontSize: 16, marginTop: 4 },
  mark: { backgroundColor: colors.goldWash },
  dim: { color: colors.dim, fontSize: 16 },
  weather: { color: colors.dim, fontSize: 16, marginTop: -8 },
  fields: { flexDirection: 'column', gap: 12 },
  editBox: {
    flexDirection: 'column',
    gap: 16,
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.gold,
    padding: 16,
    ...shadows.card,
  },
  input: {
    marginTop: 4,
    minHeight: sizes.touch,
    borderRadius: sizes.radiusSmall,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panelSoft,
    color: colors.sand,
    fontSize: 20,
    paddingHorizontal: 16,
  },
  notesInput: { minHeight: 96, paddingVertical: 12 },
  optionRow: { flexDirection: 'row', gap: 8, marginTop: 4 },
  option: {
    flex: 1,
    minHeight: sizes.touch,
    borderRadius: sizes.radiusSmall,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panelSoft,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: 8,
  },
  optionSmall: { minHeight: sizes.touch },
  optionSelected: { borderColor: colors.lightAmber, backgroundColor: colors.lightAmber },
  optionText: { color: colors.sand, fontSize: sizes.body },
  optionTextSelected: { color: colors.ink, fontSize: sizes.body, fontFamily: fonts.semibold },
  medSection: { flexDirection: 'column', gap: 12 },
  medCard: {
    flexDirection: 'column',
    gap: 8,
    borderRadius: sizes.radiusSmall,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: 12,
  },
  outcomeList: { flexDirection: 'column', gap: 8 },
  checkRow: { flexDirection: 'row', alignItems: 'center', gap: 12, minHeight: sizes.touch },
  checkbox: {
    width: 28,
    height: 28,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: colors.edge,
    alignItems: 'center',
    justifyContent: 'center',
  },
  checkboxOn: { backgroundColor: colors.lightAmber, borderColor: colors.lightAmber },
  checkMark: { color: colors.ink, fontSize: sizes.body, fontFamily: fonts.bold },
  chipRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 12,
    minHeight: sizes.touch,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: colors.edge,
    paddingHorizontal: 16,
    marginTop: 8,
  },
  chipText: { color: colors.sand, fontSize: sizes.body, flex: 1 },
  chipRemove: {
    minHeight: 48,
    minWidth: 48,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: colors.edge,
    alignItems: 'center',
    justifyContent: 'center',
  },
  addRow: { flexDirection: 'row', gap: 8, marginTop: 8, alignItems: 'center' },
  addInput: { flex: 1, marginTop: 0 },
  addButton: {
    minHeight: sizes.touch,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: colors.edge,
    paddingHorizontal: 20,
    alignItems: 'center',
    justifyContent: 'center',
  },
});
