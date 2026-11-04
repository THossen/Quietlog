/** Quietlog visual theme: premium, calm, warm dark-mode. */

export const colors = {
  night: '#111619',
  panel: '#1D252A',
  panelRaised: '#253037',
  panelSoft: '#182025',
  edge: '#344047',
  /** Top-edge highlight for elevated cards: depth without glow. */
  rise: '#52616A',
  sand: '#F5F0E8',
  dim: '#AEB8BD',
  gold: '#C49A6C',
  lightAmber: '#D5AE7D',
  ink: '#17120D',
  clay: '#C77F62',
  sage: '#91B7A2',
  goldWash: 'rgba(196, 154, 108, 0.18)',
  sageWash: 'rgba(145, 183, 162, 0.14)',
  clayWash: 'rgba(199, 127, 98, 0.14)',
} as const;

/**
 * Loaded in App via expo-font before anything renders. Body copy stays on
 * the system font; these cover display, labels, and numerals. Never pair a
 * family with fontWeight: Android cannot synthesize weights of custom
 * families, so each weight is its own family.
 */
export const fonts = {
  /** Fraunces: brand, screen titles, hero words. */
  display: 'Fraunces_600SemiBold',
  medium: 'Inter_500Medium',
  semibold: 'Inter_600SemiBold',
  bold: 'Inter_700Bold',
} as const;

/** One soft elevation, shared by panel cards. */
export const shadows = {
  card: {
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 8 },
    shadowOpacity: 0.26,
    shadowRadius: 18,
    elevation: 4,
  },
  floating: {
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 12 },
    shadowOpacity: 0.3,
    shadowRadius: 24,
    elevation: 8,
  },
};

export const sizes = {
  /** Minimum touch target (spec: at least 56px). */
  touch: 56,
  /** Body text floor (spec: at least 18px on capture screens; used everywhere). */
  body: 18,
  radius: 18,
  radiusSmall: 12,
  radiusPill: 999,
  space: 20,
} as const;
