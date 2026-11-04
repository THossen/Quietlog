import { useCallback, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { RefreshControl, ScrollView, StyleSheet, View } from 'react-native';
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
/**
 * Pull-to-refresh plumbing for tab screens: the screen body registers its
 * load function, the shell owns the spinner state. Bodies stay mounted, so
 * registration is just a ref swap whenever their load callback changes.
 */
export function usePullRefresh() {
  const [refreshing, setRefreshing] = useState(false);
  const loadRef = useRef<(() => Promise<void>) | null>(null);
  const registerRefresh = useCallback((fn: () => Promise<void>) => {
    loadRef.current = fn;
  }, []);
  const onRefresh = useCallback(async () => {
    const fn = loadRef.current;
    if (!fn || refreshing) return;
    setRefreshing(true);
    try {
      await fn();
    } finally {
      setRefreshing(false);
    }
  }, [refreshing]);
  return { refreshing, onRefresh, registerRefresh };
}

export default function ScreenShell({
  children,
  topPad = false,
  refreshing = false,
  onRefresh,
}: {
  children: ReactNode;
  topPad?: boolean;
  /** Pull-to-refresh state; only tab screens pass these. */
  refreshing?: boolean;
  onRefresh?: () => void;
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
        refreshControl={
          onRefresh ? (
            <RefreshControl
              refreshing={refreshing}
              onRefresh={onRefresh}
              tintColor={colors.lightAmber}
              colors={[colors.gold]}
            />
          ) : undefined
        }
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
