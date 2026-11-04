import type { ReactNode } from 'react';
import { ScrollView, StyleSheet, View } from 'react-native';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';
import Footer from './Footer';
import AmbientGlow from './Glow';
import { colors } from '../theme';

/**
 * Screen frame: ambient glow, scrollable body, footer. Screens stay mounted
 * in the navigator, so content refreshes on focus instead of on remount.
 *
 * Pushed screens (detail, capture) sit under a transparent native header, so
 * they pass topPad to start their content below the status bar and arrow.
 * Tab screens leave the bottom inset to the tab bar, so content scrolls
 * right up to it with no dead band in between.
 */
export default function ScreenShell({
  children,
  topPad = false,
}: {
  children: ReactNode;
  topPad?: boolean;
}) {
  const insets = useSafeAreaInsets();
  return (
    <SafeAreaView
      style={styles.safe}
      edges={topPad ? ['bottom', 'left', 'right'] : ['left', 'right']}
    >
      <AmbientGlow />
      <ScrollView
        contentContainerStyle={[styles.scroll, topPad && { paddingTop: insets.top + 48 }]}
        keyboardShouldPersistTaps="handled"
      >
        <View style={[styles.body, !topPad && styles.tabBody]}>{children}</View>
        <Footer />
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.night },
  scroll: { flexGrow: 1 },
  body: { paddingHorizontal: 20, paddingBottom: 36 },
  tabBody: { paddingBottom: 112 },
});
