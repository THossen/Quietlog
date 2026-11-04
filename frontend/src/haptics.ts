import * as Haptics from 'expo-haptics';

/** Fire-and-forget touch feedback. Never throws; silence is always safe. */
async function play(run: () => Promise<void>): Promise<void> {
  try {
    await run();
  } catch {
    /* haptics unavailable: the tap still worked */
  }
}

/** Light tick for selections and tab switches. */
export function tick(): void {
  void play(() => Haptics.selectionAsync());
}

/** Confirmed press for starting the timer and saving a note. */
export function thump(): void {
  void play(() => Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium));
}

/** Completed action: timer ended, note confirmed. */
export function done(): void {
  void play(() => Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success));
}

/** Destructive choice confirmed. */
export function warn(): void {
  void play(() => Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning));
}
