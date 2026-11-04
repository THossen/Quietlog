/**
 * One-page printable summary as self-contained HTML (M4).
 *
 * Rendered to PDF on-device with expo-print. Black on white, shapes and
 * symbols (never color alone), compact enough to fit one Letter/A4 page.
 * Every number comes from the backend's deterministic summary; this module
 * only lays them out. User-stated strings are HTML-escaped.
 */

import type { MedicationStats, Summary } from './api';
import { SYMPTOM_LABELS } from './api';

export function symptomLabel(key: string): string {
  const known = (SYMPTOM_LABELS as Record<string, string>)[key];
  if (known) return known;
  return key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

export function formatDuration(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `${hours}h` : `${hours}h ${rest}m`;
}

const OUTCOME_LABELS: Record<string, string> = {
  relief: 'relief',
  partial: 'partial',
  none: 'none',
  side_effects: 'side effects',
  recurrence: 'came back',
  unknown: 'unknown',
};

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function formatDay(isoDate: string): string {
  const [y, m, d] = isoDate.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function marker(state: string, unconfirmed: boolean): string {
  const base = state === 'attack' ? '&#9679;' : state === 'no_attack' ? '&#9675;' : '';
  return unconfirmed && state !== 'not_logged' ? `${base}*` : base;
}

function calendarTable(summary: Summary): string {
  // Align columns to real weekdays (Mon-first): pad the days before
  // window_start so a date never sits under the wrong weekday header.
  const [sy, sm, sd] = summary.window_start.split('-').map(Number);
  const lead = (new Date(sy, sm - 1, sd).getDay() + 6) % 7;
  const cells: string[] = Array.from({ length: lead }, () => '<td class="pad"></td>');
  cells.push(...summary.calendar.map((day) => {
    const [, , d] = day.day.split('-').map(Number);
    const dimmed = day.state === 'not_logged' ? ' class="dim"' : '';
    return `<td${dimmed}>${d} ${marker(day.state, day.unconfirmed)}</td>`;
  }));
  // Pad the final week so the grid stays rectangular.
  while (cells.length % 7 !== 0) cells.push('<td class="pad"></td>');
  const rows: string[] = [];
  for (let i = 0; i < cells.length; i += 7) {
    rows.push(`<tr>${cells.slice(i, i + 7).join('')}</tr>`);
  }
  return `<table class="cal" aria-label="Day-by-day record">
<thead><tr><th>Mo</th><th>Tu</th><th>We</th><th>Th</th><th>Fr</th><th>Sa</th><th>Su</th></tr></thead>
<tbody>${rows.join('')}</tbody></table>
<p class="legend">&#9679; attack day &nbsp; &#9675; no attack &nbsp; blank: not logged &nbsp; * unconfirmed</p>`;
}

function medicationRows(meds: MedicationStats[]): string {
  if (meds.length === 0) return '<p class="muted">No medication recorded in this window.</p>';
  const rows = meds
    .map((med) => {
      const outcomes = Object.entries(med.outcomes)
        .map(([outcome, count]) => `${OUTCOME_LABELS[outcome] ?? outcome}: ${count}`)
        .join('; ');
      return `<tr><td>${escapeHtml(med.display_name)}</td><td>${med.attack_entries}</td><td>${med.distinct_days}</td><td>${escapeHtml(outcomes)}</td></tr>`;
    })
    .join('');
  return `<table class="grid"><thead><tr><th>Medication</th><th>Attacks taken for</th><th>Days</th><th>Reported outcomes</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function symptomList(summary: Summary): string {
  const lines = Object.entries(summary.symptoms)
    .filter(([, count]) => count > 0)
    .map(([key, count]) => `<li>${escapeHtml(symptomLabel(key))}: mentioned in ${count} of ${summary.symptoms_n} attack entries</li>`);
  for (const [text, count] of Object.entries(summary.symptoms_other)) {
    lines.push(`<li>${escapeHtml(text)}: mentioned in ${count} of ${summary.symptoms_n} attack entries</li>`);
  }
  if (lines.length === 0) return '<p class="muted">No symptoms mentioned in this window.</p>';
  return `<ul>${lines.join('')}</ul>`;
}

function triggerList(summary: Summary): string {
  const entries = Object.entries(summary.triggers);
  if (entries.length === 0) return '<p class="muted">No triggers stated in this window.</p>';
  const items = entries.map(
    ([trigger, count]) => `<li>${escapeHtml(trigger)} — ${count} of ${summary.triggers_n} attack entries</li>`,
  );
  return `<ul>${items.join('')}</ul>`;
}

export function buildSummaryHtml(summary: Summary, narrative?: string | null): string {
  const duration =
    summary.duration.n === 0 || summary.duration.median_minutes === null
      ? 'No durations recorded.'
      : `Median ${formatDuration(summary.duration.median_minutes)} (range ${formatDuration(summary.duration.min_minutes ?? 0)}–${formatDuration(summary.duration.max_minutes ?? 0)}, ${summary.duration.n} entries).`;
  const pain =
    summary.pain.mean === null || summary.pain.n === 0
      ? 'No 0–10 pain scores recorded.'
      : `Average reported pain ${summary.pain.mean.toFixed(1)} of 10 across ${summary.pain.n} reported score${summary.pain.n === 1 ? '' : 's'} (missing scores ignored).`;
  const weather =
    summary.weather.n === 0
      ? '<p class="muted">No weather recorded for attacks in this window.</p>'
      : summary.weather.tendency_n === 0
        ? `<p>Weather recorded for ${summary.weather.n} of ${summary.symptoms_n} attack entries; no pressure trend readings.</p>`
        : `<p>Weather recorded for ${summary.weather.n} of ${summary.symptoms_n} attack entries. Pressure was falling ahead of ${summary.weather.falling_n} of ${summary.weather.tendency_n} attacks with a trend reading.</p>`;
  const severity = [1, 2, 3]
    .map(
      (level) =>
        `<li>${level === 1 ? 'Mild (could function)' : level === 2 ? 'Moderate (activities slowed)' : 'Severe (could not do activities)'}: ${summary.severity_counts[String(level)] ?? 0}</li>`,
    )
    .join('');

  return `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
<title>Quietlog summary ${escapeHtml(summary.window_start)} to ${escapeHtml(summary.window_end)}</title>
<style>
@page { size: auto; margin: 11mm 12mm; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; color: #111; font-size: 11px; line-height: 1.45; margin: 0; }
h1 { font-size: 17px; margin: 0 0 2px; }
h2 { font-size: 12.5px; margin: 12px 0 4px; border-bottom: 1px solid #999; padding-bottom: 2px; }
p, ul, table { margin: 4px 0; }
ul { padding-left: 18px; }
.sub { color: #333; }
.muted { color: #555; }
table { border-collapse: collapse; width: 100%; }
.grid th, .grid td { border: 1px solid #999; padding: 2px 6px; text-align: left; font-size: 11px; }
.grid th { background: #eee; }
.cal td, .cal th { border: 1px solid #bbb; width: 14.28%; text-align: center; padding: 2px 1px; font-size: 10.5px; }
.cal th { background: #eee; font-weight: 600; }
.cal td.dim { color: #888; }
.cal td.pad { border: none; }
.legend { font-size: 10px; color: #333; }
.counts { font-size: 11.5px; }
.footer { margin-top: 10px; border-top: 1px solid #999; padding-top: 4px; font-size: 10px; color: #333; }
</style></head><body>
<h1>Quietlog summary: ${escapeHtml(formatDay(summary.window_start))} – ${escapeHtml(formatDay(summary.window_end))}</h1>
<p class="sub">Generated ${escapeHtml(formatDateTime(summary.generated_at))}. Patient-recorded information. Not a medical record or a diagnosis.</p>
<h2>Days</h2>
<p class="counts">${summary.attack_days} days with an attack entry &nbsp;·&nbsp; ${summary.no_attack_days} days with a “no attack” check-in &nbsp;·&nbsp; ${summary.not_logged_days} days not logged &nbsp;·&nbsp; ${summary.unconfirmed_entries} unconfirmed ${summary.unconfirmed_entries === 1 ? 'entry' : 'entries'} (${summary.attack_entries} attack ${summary.attack_entries === 1 ? 'entry' : 'entries'} total).</p>
<h2>Day by day</h2>
${calendarTable(summary)}
<h2>Duration, pain, and severity</h2>
<p>${escapeHtml(duration)}</p>
<p>${escapeHtml(pain)}</p>
<ul>${severity}</ul>
<h2>Symptoms</h2>
${symptomList(summary)}
<h2>Acute medication</h2>
${medicationRows(summary.medications)}
<h2>Reported triggers</h2>
${triggerList(summary)}
<h2>Weather</h2>
${weather}
${narrative ? `<h2>Summary paragraph</h2>\n<p class="muted">Drafted with AI assistance from the numbers above; reviewed by the patient before sharing.</p>\n<p>${escapeHtml(narrative)}</p>` : ''}
<p class="footer">Counts come from entries the user recorded. Days not logged are unknown, not attack-free.</p>
</body></html>`;
}
