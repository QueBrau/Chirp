import { useEffect, useState } from "react";
import { View } from "react-native";
import { acceptLegal, getLegalStatus, type LegalPolicyStatus } from "@/api/auth";
import { AppText, Button, Screen } from "@/components";
import { useSession } from "@/auth";
import { spacing, useTheme } from "@/theme";

export default function LegalAcceptanceScreen() {
  const { refresh } = useSession();
  const palette = useTheme();
  const [status, setStatus] = useState<LegalPolicyStatus | null>(null);
  const [age, setAge] = useState<17 | 18 | null>(null);
  const [guardian, setGuardian] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { void getLegalStatus().then(setStatus).catch(() => setError("Couldn't load the current policies.")); }, []);
  const submit = async () => {
    if (!status || age === null || (age === 17 && !guardian)) { setError("Choose your age category, and confirm a parent or guardian gave permission if you are 17."); return; }
    try {
      const terms = status.policies.find(p => p.key === "terms");
      const privacy = status.policies.find(p => p.key === "privacy");
      if (!terms || !privacy) throw new Error("missing policy");
      await acceptLegal({ terms_version: terms.version, privacy_version: privacy.version, age_declaration: age, guardian_permission_confirmed: guardian });
      await refresh();
    } catch { setError("Couldn't save your acceptance. Please try again."); }
  };
  return <Screen title="A quick check before you continue" subtitle="Chirp is for students and alumni. Please acknowledge the current Terms and Privacy Policy.">
    <View style={{ gap: spacing.md }}>
      <Button label="I am 18 or older" variant={age === 18 ? "primary" : "secondary"} onPress={() => { setAge(18); setGuardian(false); }} />
      <Button label="I am 17" variant={age === 17 ? "primary" : "secondary"} onPress={() => setAge(17)} />
      {age === 17 ? <Button label={guardian ? "Parent or guardian permission confirmed" : "Confirm parent or guardian permission"} variant="secondary" onPress={() => setGuardian(v => !v)} /> : null}
      <AppText variant="caption" tone="secondary">We store this acknowledgement, its time, your account, and the age category you selected.</AppText>
      {error ? <AppText variant="caption" style={{ color: palette.danger }}>{error}</AppText> : null}
      <Button label="Continue" onPress={() => void submit()} disabled={!status} />
    </View>
  </Screen>;
}
