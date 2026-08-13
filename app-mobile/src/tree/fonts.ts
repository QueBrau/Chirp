/** Muli typeface for the family-tree graph screen. */

import {
  Muli_400Regular,
  Muli_500Medium,
  Muli_600SemiBold,
  Muli_700Bold,
  useFonts,
} from "@expo-google-fonts/muli";

export const muli = {
  regular: "Muli_400Regular",
  medium: "Muli_500Medium",
  semibold: "Muli_600SemiBold",
  bold: "Muli_700Bold",
} as const;

/** Load Muli weights used by the lineage graph + inspector. Returns true when ready. */
export function useMuliFonts(): boolean {
  const [loaded] = useFonts({
    Muli_400Regular,
    Muli_500Medium,
    Muli_600SemiBold,
    Muli_700Bold,
  });
  return loaded;
}
