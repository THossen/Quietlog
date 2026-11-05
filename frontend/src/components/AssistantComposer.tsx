import { forwardRef, type ComponentRef } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import Reanimated, { useAnimatedStyle, useSharedValue, withTiming } from 'react-native-reanimated';
import { ArrowUp, Mic, Square } from 'lucide-react-native';
import { formatClock } from '../audio';
import { useReducedMotionSetting } from '../motion';
import { colors, fonts } from '../theme';

interface AssistantComposerProps {
  value: string;
  onChangeText: (value: string) => void;
  onSend: () => void;
  onStartRecording: () => void;
  onStopRecording: () => void;
  busy: boolean;
  startingVoice: boolean;
  recording: boolean;
  recordingSeconds: number;
  transcribing: boolean;
}

/** Text gets the full width; controls stay put as a draft grows to several lines. */
const AssistantComposer = forwardRef<ComponentRef<typeof TextInput>, AssistantComposerProps>(
  function AssistantComposer({
    value,
    onChangeText,
    onSend,
    onStartRecording,
    onStopRecording,
    busy,
    startingVoice,
    recording,
    recordingSeconds,
    transcribing,
  }, ref) {
    const reduceMotion = useReducedMotionSetting();
    const focused = useSharedValue(false);
    // Keep focus feedback off React's render path while the keyboard opens.
    const focusStyle = useAnimatedStyle(() => ({
      borderColor: withTiming(focused.value ? colors.rise : colors.edge, {
        duration: reduceMotion ? 0 : 120,
      }),
    }));
    const micDisabled = busy || transcribing || startingVoice;
    const sendDisabled = !value.trim() || micDisabled || recording;
    const voicePending = startingVoice || transcribing;

    return (
      <Reanimated.View style={[styles.composer, focusStyle]}>
        {recording ? (
          <View style={styles.recordingRow}>
            <View style={styles.recordingDot} />
            <Text style={styles.recordingLabel}>Recording</Text>
            <Text style={styles.recordingTime}>{formatClock(recordingSeconds)}</Text>
          </View>
        ) : (
          <TextInput
            ref={ref}
            value={value}
            onChangeText={onChangeText}
            onFocus={() => { focused.value = true; }}
            onBlur={() => { focused.value = false; }}
            accessibilityLabel="Message your diary assistant"
            placeholder="Message Quietlog"
            placeholderTextColor={colors.dim}
            keyboardAppearance="dark"
            selectionColor={colors.gold}
            cursorColor={colors.gold}
            style={styles.input}
            maxLength={1000}
            multiline
            editable={!transcribing && !startingVoice}
            textAlignVertical="top"
          />
        )}
        <View style={styles.toolbar}>
          <View style={styles.status}>
            {recording ? <Text style={styles.statusText}>Tap stop when you're done</Text> : null}
            {voicePending ? (
              <Text accessibilityLiveRegion="polite" style={styles.statusText}>
                {transcribing ? 'Transcribing…' : 'Starting microphone…'}
              </Text>
            ) : null}
          </View>
          <View style={styles.actions}>
            {!recording ? (
              <Pressable
                onPress={onStartRecording}
                hitSlop={{ top: 4, bottom: 4, left: 4, right: 4 }}
                accessibilityRole="button"
                accessibilityLabel="Record a voice message"
                accessibilityState={{ disabled: micDisabled, busy: voicePending }}
                disabled={micDisabled}
                style={({ pressed }) => [styles.control, pressed && styles.pressed]}
              >
                {voicePending ? (
                  <ActivityIndicator size="small" color={colors.dim} />
                ) : (
                  <Mic size={21} color={micDisabled ? colors.dim : colors.sand} strokeWidth={1.8} />
                )}
              </Pressable>
            ) : null}
            <Pressable
              onPress={recording ? onStopRecording : onSend}
              hitSlop={{ top: 4, bottom: 4, left: 4, right: 4 }}
              accessibilityRole="button"
              accessibilityLabel={recording ? 'Stop recording' : 'Send message'}
              accessibilityState={{ disabled: !recording && sendDisabled }}
              disabled={!recording && sendDisabled}
              style={({ pressed }) => [styles.control, pressed && styles.pressed]}
            >
              <View style={[styles.primaryCircle, !recording && sendDisabled && styles.primaryDisabled]}>
                {recording ? (
                  <Square size={16} color={colors.ink} fill={colors.ink} />
                ) : (
                  <ArrowUp size={21} color={sendDisabled ? colors.dim : colors.ink} strokeWidth={2.2} />
                )}
              </View>
            </Pressable>
          </View>
        </View>
      </Reanimated.View>
    );
  },
);

export default AssistantComposer;

const styles = StyleSheet.create({
  composer: {
    marginTop: 10,
    borderRadius: 24,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panelRaised,
    padding: 6,
  },
  input: {
    minHeight: 36,
    maxHeight: 144,
    color: colors.sand,
    fontSize: 17,
    lineHeight: 24,
    paddingHorizontal: 10,
    paddingTop: 6,
    paddingBottom: 6,
  },
  toolbar: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    minHeight: 36,
    marginTop: 4,
  },
  status: { flex: 1, paddingLeft: 10, paddingRight: 8 },
  statusText: { color: colors.dim, fontSize: 13, lineHeight: 18 },
  actions: { flexDirection: 'row', alignItems: 'center', gap: 4 },
  control: { width: 36, height: 36, alignItems: 'center', justifyContent: 'center', borderRadius: 18 },
  pressed: { opacity: 0.65 },
  primaryCircle: { width: 36, height: 36, borderRadius: 18, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.sand },
  primaryDisabled: { backgroundColor: colors.edge },
  recordingRow: { flexDirection: 'row', alignItems: 'center', minHeight: 36, paddingHorizontal: 10, gap: 10 },
  recordingDot: { width: 8, height: 8, borderRadius: 4, backgroundColor: colors.clay },
  recordingLabel: { flex: 1, color: colors.sand, fontSize: 17 },
  recordingTime: { color: colors.dim, fontFamily: fonts.medium, fontSize: 15, fontVariant: ['tabular-nums'] },
});
