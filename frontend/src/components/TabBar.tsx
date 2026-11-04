import { StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { ChartLine, FileText, History, House, Sparkles } from 'lucide-react-native';
import type { LucideIcon } from 'lucide-react-native';
import type { BottomTabBarProps } from '@react-navigation/bottom-tabs';
import { tick } from '../haptics';
import { colors, fonts } from '../theme';
import { ScalePressable } from './ui';

export type Tab = 'home' | 'history' | 'insights' | 'reports';

const TABS: Array<{ id: Tab; label: string; icon: LucideIcon }> = [
  { id: 'home', label: 'Home', icon: House },
  { id: 'history', label: 'History', icon: History },
  { id: 'insights', label: 'Insights', icon: ChartLine },
  { id: 'reports', label: 'Reports', icon: FileText },
];

export default function TabBar({
  tab,
  onTab,
  onAssistant,
}: {
  tab: Tab;
  onTab: (tab: Tab) => void;
  onAssistant: () => void;
}) {
  const insets = useSafeAreaInsets();
  const renderTab = (item: (typeof TABS)[number]) => {
    const active = tab === item.id;
    const color = active ? colors.lightAmber : colors.dim;
    const Icon = item.icon;
    return (
      <ScalePressable
        key={item.id}
        onPress={() => {
          tick();
          onTab(item.id);
        }}
        accessibilityRole="tab"
        accessibilityLabel={item.label}
        accessibilityState={{ selected: active }}
        style={[styles.tab, active && styles.activeTab]}
      >
        <View style={styles.iconFrame}>
          <Icon size={21} color={color} strokeWidth={active ? 2.2 : 1.8} />
        </View>
        <Text style={active ? styles.activeText : styles.text}>{item.label}</Text>
      </ScalePressable>
    );
  };
  return (
    <View
      pointerEvents="box-none"
      style={[styles.bar, { paddingBottom: Math.max(insets.bottom, 6) }]}
    >
      <View style={styles.dock}>
        {renderTab(TABS[0])}
        {renderTab(TABS[1])}
        <View style={styles.fabSlot} accessible={false} />
        {renderTab(TABS[2])}
        {renderTab(TABS[3])}
        <ScalePressable
          onPress={() => {
            tick();
            onAssistant();
          }}
          accessibilityRole="button"
          accessibilityLabel="Open assistant"
          style={styles.fab}
        >
          <Sparkles size={25} color={colors.ink} strokeWidth={2.1} />
        </ScalePressable>
      </View>
    </View>
  );
}

const ROUTE_TO_TAB: Record<string, Tab> = {
  Home: 'home',
  History: 'history',
  Insights: 'insights',
  Reports: 'reports',
};

const TAB_TO_ROUTE: Record<Tab, string> = {
  home: 'Home',
  history: 'History',
  insights: 'Insights',
  reports: 'Reports',
};

/** Navigator-driven wrapper: maps tab state and events onto the Quietlog bar. */
export function NavigatorTabBar({
  state,
  navigation,
  onAssistant,
}: BottomTabBarProps & { onAssistant: () => void }) {
  const activeRoute = state.routes[state.index];
  return (
    <TabBar
      tab={ROUTE_TO_TAB[activeRoute?.name ?? 'Home'] ?? 'home'}
      onAssistant={onAssistant}
      onTab={(tab) => {
        const name = TAB_TO_ROUTE[tab];
        const target = state.routes.find((r) => r.name === name);
        if (!target) return;
        const event = navigation.emit({
          type: 'tabPress',
          target: target.key,
          canPreventDefault: true,
        });
        if (state.index !== state.routes.indexOf(target) && !event.defaultPrevented) {
          navigation.navigate(name);
        }
      }}
    />
  );
}

const styles = StyleSheet.create({
  bar: {
    // Floating dock: hovers over scrolling content (custom tab bars ignore
    // the navigator's tabBarStyle, so this positioning lives here). The top
    // padding keeps the raised FAB inside the touchable bounds; box-none lets
    // touches on the transparent margins fall through to content behind.
    position: 'absolute',
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: colors.night,
    borderTopWidth: 1,
    borderTopColor: colors.edge,
    paddingTop: 14,
    paddingHorizontal: 18,
  },
  dock: {
    flexDirection: 'row',
    minHeight: 68,
    borderRadius: 22,
    borderWidth: 1,
    borderColor: 'rgba(82, 97, 106, 0.72)',
    backgroundColor: 'rgba(29, 37, 42, 0.96)',
    paddingHorizontal: 5,
    paddingTop: 4,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 6 },
    shadowOpacity: 0.24,
    shadowRadius: 16,
    elevation: 6,
  },
  tab: {
    flex: 1,
    minHeight: 57,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 2,
    borderRadius: 16,
    marginBottom: 4,
  },
  activeTab: {
    backgroundColor: colors.goldWash,
  },
  iconFrame: {
    height: 27,
    alignItems: 'center',
    justifyContent: 'center',
  },
  text: { color: colors.dim, fontSize: 12, fontFamily: fonts.medium },
  activeText: { color: colors.lightAmber, fontSize: 12, fontFamily: fonts.semibold },
  fabSlot: { flex: 1, minHeight: 57 },
  fab: {
    position: 'absolute',
    left: '50%',
    marginLeft: -29,
    top: -23,
    width: 58,
    height: 58,
    borderRadius: 29,
    backgroundColor: colors.gold,
    borderWidth: 4,
    borderColor: colors.night,
    alignItems: 'center',
    justifyContent: 'center',
    zIndex: 1,
    shadowColor: colors.gold,
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.28,
    shadowRadius: 9,
    elevation: 8,
  },
});
