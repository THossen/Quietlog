import { useEffect } from 'react';
import { StyleSheet, Text, View, useColorScheme } from 'react-native';
import { SafeAreaProvider, SafeAreaView, initialWindowMetrics } from 'react-native-safe-area-context';
import { StatusBar } from 'expo-status-bar';
import { Fraunces_600SemiBold } from '@expo-google-fonts/fraunces';
import { Inter_500Medium, Inter_600SemiBold, Inter_700Bold } from '@expo-google-fonts/inter';
import { useFonts } from 'expo-font';
import * as SplashScreen from 'expo-splash-screen';
import { MoonStar } from 'lucide-react-native';
import { DarkTheme, DefaultTheme, NavigationContainer, useNavigation } from '@react-navigation/native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import {
  createNativeStackNavigator,
  type NativeStackNavigationProp,
} from '@react-navigation/native-stack';
import { NavigatorTabBar } from './src/components/TabBar';
import { ChatProvider } from './src/chat';
import type { RootStackParamList, TabParamList } from './src/nav';
import Assistant from './src/screens/Assistant';
import Capture from './src/screens/Capture';
import Home from './src/screens/Home';
import Insights from './src/screens/Insights';
import ReviewDetail from './src/screens/ReviewDetail';
import ReviewList from './src/screens/ReviewList';
import Summary from './src/screens/Summary';
import { colors, fonts } from './src/theme';

void SplashScreen.preventAutoHideAsync().catch(() => {
  /* splash already hidden (e.g. Fast Refresh): render anyway */
});

const Tab = createBottomTabNavigator<TabParamList>();
const Stack = createNativeStackNavigator<RootStackParamList>();

function BrandBar() {
  return (
    <SafeAreaView edges={['top', 'left', 'right']} style={styles.brandSafe}>
      <View style={styles.brand}>
        <View style={styles.brandMark}>
          <MoonStar size={20} color={colors.gold} strokeWidth={2.2} />
        </View>
        <View style={styles.brandCopy}>
          <Text style={styles.brandEyebrow}>PRIVATE JOURNAL</Text>
          <Text style={styles.brandTitle}>Quietlog</Text>
        </View>
        <View style={styles.brandRule} />
      </View>
    </SafeAreaView>
  );
}

function Tabs() {
  const stack = useNavigation<NativeStackNavigationProp<RootStackParamList>>();
  return (
    <>
      <BrandBar />
      <Tab.Navigator
        tabBar={(props) => (
          <NavigatorTabBar {...props} onAssistant={() => stack.navigate('Assistant')} />
        )}
        screenOptions={{
          headerShown: false,
          tabBarStyle: {
            position: 'absolute',
            backgroundColor: 'transparent',
            borderTopWidth: 0,
            elevation: 0,
          },
        }}
      >
        <Tab.Screen name="Home" component={Home} />
        <Tab.Screen name="History" component={ReviewList} />
        <Tab.Screen name="Insights" component={Insights} />
        <Tab.Screen name="Reports" component={Summary} />
      </Tab.Navigator>
    </>
  );
}

export default function App() {
  const systemScheme = useColorScheme();
  const [fontsLoaded] = useFonts({
    Fraunces_600SemiBold,
    Inter_500Medium,
    Inter_600SemiBold,
    Inter_700Bold,
  });

  useEffect(() => {
    if (fontsLoaded) {
      void SplashScreen.hideAsync().catch(() => {
        /* already hidden */
      });
    }
  }, [fontsLoaded]);

  if (!fontsLoaded) return null;

  return (
    <SafeAreaProvider initialMetrics={initialWindowMetrics}>
      <View style={styles.root}>
        <StatusBar style="light" />
        <ChatProvider>
        <NavigationContainer theme={systemScheme === 'dark' ? DarkTheme : DefaultTheme}>
          <Stack.Navigator
            screenOptions={{
              headerShown: false,
              gestureEnabled: true,
              fullScreenGestureEnabled: true,
            }}
          >
            <Stack.Screen name="Tabs" component={Tabs} />
            <Stack.Screen
              name="Detail"
              component={ReviewDetail}
              options={{
                headerShown: true,
                headerTransparent: true,
                headerTitle: '',
                headerBackButtonDisplayMode: 'minimal',
                headerTintColor: colors.sand,
              }}
            />
            <Stack.Screen
              name="Capture"
              component={Capture}
              options={{
                headerShown: true,
                headerTransparent: true,
                headerTitle: '',
                headerBackButtonDisplayMode: 'minimal',
                headerTintColor: colors.sand,
              }}
            />
            <Stack.Screen
              name="Assistant"
              component={Assistant}
              options={{
                presentation: 'fullScreenModal',
                animation: 'slide_from_bottom',
                headerShown: false,
                gestureEnabled: true,
              }}
            />
          </Stack.Navigator>
        </NavigationContainer>
        </ChatProvider>
      </View>
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.night },
  brandSafe: { backgroundColor: colors.night },
  brand: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 11,
    paddingHorizontal: 18,
    paddingTop: 12,
    paddingBottom: 14,
    borderBottomWidth: 1,
    borderBottomColor: colors.edge,
  },
  brandMark: {
    width: 38,
    height: 38,
    borderRadius: 13,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.goldWash,
    borderWidth: 1,
    borderColor: colors.gold,
  },
  brandCopy: {
    justifyContent: 'center',
    gap: 1,
  },
  brandEyebrow: {
    color: colors.dim,
    fontFamily: fonts.semibold,
    fontSize: 9,
    letterSpacing: 1.6,
  },
  brandRule: {
    flex: 1,
    height: 1,
    marginLeft: 8,
    backgroundColor: colors.edge,
  },
  brandTitle: {
    fontFamily: fonts.display,
    fontSize: 25,
    lineHeight: 28,
    color: colors.sand,
  },
});
