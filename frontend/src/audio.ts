/**
 * Phone audio: capture microphone PCM with expo-audio's native stream and
 * guarantee a 16 kHz mono 16-bit WAV for the transcription endpoint.
 *
 * The stream is asked for exactly that format; when the hardware delivers
 * something else, the file is converted in plain JS (no native modules
 * beyond expo-audio and expo-file-system, so this runs in Expo Go).
 */

import { requestRecordingPermissionsAsync, useAudioStream } from 'expo-audio';
import type { AudioStream, AudioStreamFileRecordingResult } from 'expo-audio';
import { EncodingType, File, Paths, writeAsStringAsync } from 'expo-file-system';

export const TARGET_RATE = 16000;

export { useAudioStream };
export type { AudioStream };

export async function ensureMicPermission(): Promise<void> {
  const { granted } = await requestRecordingPermissionsAsync();
  if (!granted) {
    throw new Error('Microphone access was denied. Type your note instead.');
  }
}

async function readBytes(uri: string): Promise<Uint8Array> {
  const buffer = await new File(uri).arrayBuffer();
  return new Uint8Array(buffer);
}

function bytesToBase64(bytes: Uint8Array): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  let out = '';
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i];
    const b = i + 1 < bytes.length ? bytes[i + 1] : 0;
    const c = i + 2 < bytes.length ? bytes[i + 2] : 0;
    const triple = (a << 16) | (b << 8) | c;
    out += alphabet[(triple >> 18) & 63] + alphabet[(triple >> 12) & 63];
    out += i + 1 < bytes.length ? alphabet[(triple >> 6) & 63] : '=';
    out += i + 2 < bytes.length ? alphabet[triple & 63] : '=';
  }
  return out;
}

async function writeBytes(uri: string, bytes: Uint8Array): Promise<void> {
  const file = new File(uri);
  try {
    const writer = file.writableStream().getWriter();
    await writer.write(bytes);
    await writer.close();
  } catch {
    // Older runtime without FileHandle writes: legacy base64 write.
    await writeAsStringAsync(file.uri, bytesToBase64(bytes), { encoding: EncodingType.Base64 });
  }
}

interface DecodedWav {
  samples: Float32Array; // mono, -1..1
  sampleRate: number;
}

function ascii(view: DataView, offset: number, length: number): string {
  let text = '';
  for (let i = 0; i < length; i += 1) text += String.fromCharCode(view.getUint8(offset + i));
  return text;
}

/** Parse a PCM WAV (int16 or float32, any rate/channels) into mono Float32. */
function decodeWav(bytes: Uint8Array): DecodedWav {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (bytes.length < 44 || ascii(view, 0, 4) !== 'RIFF' || ascii(view, 8, 4) !== 'WAVE') {
    throw new Error('The recording came back in an unexpected format.');
  }
  // Walk chunks (fmt, then data; tolerate extras like LIST).
  let offset = 12;
  let format = 0;
  let channels = 0;
  let sampleRate = 0;
  let bits = 0;
  let dataStart = 0;
  let dataLength = 0;
  while (offset + 8 <= bytes.length) {
    const id = ascii(view, offset, 4);
    const size = view.getUint32(offset + 4, true);
    if (id === 'fmt ') {
      format = view.getUint16(offset + 8, true);
      channels = view.getUint16(offset + 10, true);
      sampleRate = view.getUint32(offset + 12, true);
      bits = view.getUint16(offset + 22, true);
    } else if (id === 'data') {
      dataStart = offset + 8;
      dataLength = Math.min(size, bytes.length - dataStart);
    }
    offset += 8 + size + (size % 2);
  }
  if (channels < 1 || sampleRate <= 0 || dataLength <= 0) {
    throw new Error('The recording came back in an unexpected format.');
  }
  const bytesPerSample = bits / 8;
  if (!((format === 1 && bits === 16) || (format === 3 && bits === 32)) || bytesPerSample <= 0) {
    throw new Error('The recording came back in an unexpected format.');
  }
  const frames = Math.floor(dataLength / (bytesPerSample * channels));
  const mono = new Float32Array(frames);
  for (let frame = 0; frame < frames; frame += 1) {
    let sum = 0;
    for (let ch = 0; ch < channels; ch += 1) {
      const at = dataStart + (frame * channels + ch) * bytesPerSample;
      sum += format === 1 ? view.getInt16(at, true) / 0x8000 : view.getFloat32(at, true);
    }
    mono[frame] = sum / channels;
  }
  return { samples: mono, sampleRate };
}

function resampleMono(samples: Float32Array, fromRate: number): Float32Array {
  if (fromRate === TARGET_RATE) return samples;
  const outLength = Math.max(1, Math.floor((samples.length * TARGET_RATE) / fromRate));
  const out = new Float32Array(outLength);
  for (let i = 0; i < outLength; i += 1) {
    const pos = (i * fromRate) / TARGET_RATE;
    const j = Math.floor(pos);
    const frac = pos - j;
    const a = samples[Math.min(j, samples.length - 1)];
    const b = samples[Math.min(j + 1, samples.length - 1)];
    out[i] = a + (b - a) * frac;
  }
  return out;
}

function encodeWav16k(samples: Float32Array): Uint8Array {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeAscii = (at: number, text: string) => {
    for (let i = 0; i < text.length; i += 1) view.setUint8(at + i, text.charCodeAt(i));
  };
  writeAscii(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeAscii(8, 'WAVE');
  writeAscii(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, TARGET_RATE, true);
  view.setUint32(28, TARGET_RATE * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
  }
  return new Uint8Array(buffer);
}

/**
 * Return a file URI holding 16 kHz mono 16-bit WAV: the recorded file
 * itself when the hardware delivered exactly that, else a converted copy.
 */
export async function ensureWav16k(result: AudioStreamFileRecordingResult): Promise<string> {
  if (result.sampleRate === TARGET_RATE && result.channels === 1 && result.encoding === 'int16') {
    return result.uri;
  }
  const bytes = await readBytes(result.uri);
  const decoded = decodeWav(bytes);
  const converted = encodeWav16k(resampleMono(decoded.samples, decoded.sampleRate));
  const outUri = new File(Paths.cache, `note-16k-${Date.now()}.wav`).uri;
  await writeBytes(outUri, converted);
  return outUri;
}

export function formatClock(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}
