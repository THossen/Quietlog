import * as Location from 'expo-location';
import type { Coords } from './api';

/**
 * One-shot location for a log or timer tap, used only to fetch the weather
 * for that moment. Silent by design: denied, unavailable, or slow location
 * yields null and the entry simply saves without weather.
 */
export async function getLogCoords(): Promise<Coords | null> {
  try {
    const current = await Location.getForegroundPermissionsAsync();
    let granted = current.granted;
    if (!granted && current.canAskAgain) {
      granted = (await Location.requestForegroundPermissionsAsync()).granted;
    }
    if (!granted) return null;
    const position = await Promise.race([
      Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.Balanced }),
      new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error('location timed out')), 8000),
      ),
    ]);
    const { latitude, longitude } = position.coords;
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
    return { latitude, longitude };
  } catch {
    return null;
  }
}
