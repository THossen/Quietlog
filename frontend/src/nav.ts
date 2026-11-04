/** Navigator param lists: bottom tabs plus the pushed stack screens. */

export type TabParamList = {
  Home: undefined;
  History: undefined;
  Insights: undefined;
  Reports: undefined;
};

export type RootStackParamList = {
  Tabs: undefined;
  Detail: { id: string };
  Capture: { entryId?: string };
  Assistant: undefined;
};
