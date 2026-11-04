import { useRef } from 'react';
import type { ComponentProps, ReactNode } from 'react';
import type { StyleProp, ViewStyle } from 'react-native';
import { Animated, Pressable, StyleSheet, Text, View } from 'react-native';
import { useReducedMotionSetting } from '../motion';
import { colors, fonts, shadows, sizes } from '../theme';

const AnimatedPressable = Animated.createAnimatedComponent(Pressable);

type PressableProps = ComponentProps<typeof Pressable>;

/**
 * Pressable with a gentle press-scale. Skipped under reduced motion.
 * Buttons and heroes use this; small toggles keep their selected states.
 */
export function ScalePressable({
  children,
  style,
  ...rest
}: Omit<PressableProps, 'style'> & { style?: StyleProp<ViewStyle> }) {
  const scale = useRef(new Animated.Value(1)).current;
  const reduceMotion = useReducedMotionSetting();
  const settle = (to: number) => {
    if (reduceMotion) return;
    Animated.spring(scale, {
      toValue: to,
      useNativeDriver: true,
      speed: 50,
      bounciness: 0,
    }).start();
  };
  return (
    <AnimatedPressable
      {...rest}
      onPressIn={(event) => {
        settle(0.97);
        rest.onPressIn?.(event);
      }}
      onPressOut={(event) => {
        settle(1);
        rest.onPressOut?.(event);
      }}
      style={[style, { transform: [{ scale }] }]}
    >
      {children}
    </AnimatedPressable>
  );
}

export function PrimaryButton({
  title,
  onPress,
  disabled,
  accessibilityLabel,
}: {
  title: string;
  onPress: () => void;
  disabled?: boolean;
  accessibilityLabel?: string;
}) {
  return (
    <ScalePressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? title}
      accessibilityState={{ disabled: disabled === true }}
      style={[styles.primary, disabled ? styles.disabled : null]}
    >
      <Text style={styles.primaryText} numberOfLines={1} adjustsFontSizeToFit minimumFontScale={0.8}>
        {title}
      </Text>
    </ScalePressable>
  );
}

export function GhostButton({
  title,
  onPress,
  disabled,
  accessibilityLabel,
}: {
  title: string;
  onPress: () => void;
  disabled?: boolean;
  accessibilityLabel?: string;
}) {
  return (
    <ScalePressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? title}
      accessibilityState={{ disabled: disabled === true }}
      style={[styles.ghost, disabled ? styles.disabled : null]}
    >
      <Text style={styles.ghostText} numberOfLines={1} adjustsFontSizeToFit minimumFontScale={0.8}>
        {title}
      </Text>
    </ScalePressable>
  );
}

export function ErrorText({ children }: { children: ReactNode }) {
  return (
    <Text role="alert" style={styles.error}>
      {children}
    </Text>
  );
}

export function StatusBadge({ tone, children }: { tone: 'gold' | 'clay' | 'dim' | 'sage'; children: ReactNode }) {
  return (
    <View
      style={[
        styles.badge,
        tone === 'gold'
          ? styles.badgeGold
          : tone === 'clay'
            ? styles.badgeClay
            : tone === 'sage'
              ? styles.badgeSage
              : styles.badgeDim,
      ]}
    >
      <Text
        style={[
          styles.badgeText,
          tone === 'gold'
            ? styles.goldText
            : tone === 'clay'
              ? styles.clayText
              : tone === 'sage'
                ? styles.sageText
                : styles.dimText,
        ]}
      >
        {children}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  primary: {
    minHeight: sizes.touch,
    borderRadius: sizes.radius,
    backgroundColor: colors.gold,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: 20,
    paddingVertical: 12,
    ...shadows.card,
  },
  disabled: { opacity: 0.5 },
  primaryText: { color: colors.ink, fontSize: 18, fontFamily: fonts.semibold, letterSpacing: 0.1 },
  ghost: {
    minHeight: sizes.touch,
    borderRadius: sizes.radius,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.panel,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: 20,
    paddingVertical: 12,
  },
  ghostText: { color: colors.sand, fontSize: 17, fontFamily: fonts.medium },
  error: {
    color: colors.clay,
    fontSize: 16,
    lineHeight: 23,
    textAlign: 'center',
    backgroundColor: colors.clayWash,
    borderRadius: sizes.radiusSmall,
    paddingHorizontal: 14,
    paddingVertical: 10,
  },
  badge: {
    borderWidth: 1,
    borderRadius: sizes.radiusPill,
    paddingHorizontal: 11,
    paddingVertical: 5,
    backgroundColor: colors.panelSoft,
  },
  badgeGold: { borderColor: colors.gold },
  badgeClay: { borderColor: colors.clay },
  badgeDim: { borderColor: colors.edge },
  badgeSage: { borderColor: colors.sage },
  badgeText: { fontSize: 13, fontFamily: fonts.medium },
  goldText: { color: colors.gold },
  clayText: { color: colors.clay },
  dimText: { color: colors.dim },
  sageText: { color: colors.sage },
});
