import { StyleSheet } from 'react-native';
import Svg, { Defs, RadialGradient, Rect, Stop } from 'react-native-svg';
import { colors } from '../theme';

/**
 * One static warm wash behind the whole app. Barely-there by design:
 * atmosphere, not a gradient feature. Static, so reduced-motion safe.
 */
export default function AmbientGlow() {
  return (
    <Svg
      style={styles.fill}
      pointerEvents="none"
      accessibilityElementsHidden
      importantForAccessibility="no-hide-descendants"
    >
      <Defs>
        <RadialGradient id="quietlog-glow" cx="50%" cy="0%" r="75%">
          <Stop offset="0%" stopColor={colors.gold} stopOpacity={0.14} />
          <Stop offset="100%" stopColor={colors.gold} stopOpacity={0} />
        </RadialGradient>
      </Defs>
      <Rect x="0" y="0" width="100%" height="100%" fill="url(#quietlog-glow)" />
    </Svg>
  );
}

const styles = StyleSheet.create({
  fill: { position: 'absolute', width: '100%', height: '100%' },
});
