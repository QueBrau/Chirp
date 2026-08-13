/** Alumni / e-board form to post a hiring role to the chapter job board. */

import { useState } from "react";
import { TextInput, View } from "react-native";
import { useRouter } from "expo-router";

import { createJob } from "@/api/alumni";
import { AppText, Button, Screen } from "@/components";
import { MOCK_CURRENT_MEMBERSHIP } from "@/mocks/data";
import { radii, spacing, typography, useTheme } from "@/theme";

export default function PostJobScreen() {
  const palette = useTheme();
  const router = useRouter();
  const [title, setTitle] = useState("");
  const [company, setCompany] = useState("");
  const [location, setLocation] = useState("");
  const [description, setDescription] = useState("");
  const [applyUrl, setApplyUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const inputStyle = {
    ...typography.body,
    color: palette.textPrimary,
    backgroundColor: palette.surface,
    borderWidth: 1,
    borderColor: palette.border,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.md,
  };

  const canSubmit =
    title.trim().length > 0 &&
    company.trim().length > 0 &&
    location.trim().length > 0 &&
    description.trim().length > 0 &&
    !submitting;

  const onSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      await createJob({
        chapter_id: MOCK_CURRENT_MEMBERSHIP.chapter_id,
        title: title.trim(),
        company: company.trim(),
        location: location.trim(),
        description: description.trim(),
        apply_url: applyUrl.trim() || null,
      });
      router.back();
    } catch {
      setError("Could not post this job. Try again.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Screen title="Post a job" subtitle="Share an opening with the chapter">
      <View style={{ gap: spacing.md }}>
        <View style={{ gap: spacing.xs }}>
          <AppText variant="caption" tone="tertiary">
            ROLE TITLE
          </AppText>
          <TextInput
            value={title}
            onChangeText={setTitle}
            placeholder="e.g. Summer Analyst Intern"
            placeholderTextColor={palette.textTertiary}
            style={inputStyle}
          />
        </View>

        <View style={{ gap: spacing.xs }}>
          <AppText variant="caption" tone="tertiary">
            COMPANY
          </AppText>
          <TextInput
            value={company}
            onChangeText={setCompany}
            placeholder="Where you're hiring"
            placeholderTextColor={palette.textTertiary}
            style={inputStyle}
          />
        </View>

        <View style={{ gap: spacing.xs }}>
          <AppText variant="caption" tone="tertiary">
            LOCATION
          </AppText>
          <TextInput
            value={location}
            onChangeText={setLocation}
            placeholder="City, hybrid, or Remote"
            placeholderTextColor={palette.textTertiary}
            style={inputStyle}
          />
        </View>

        <View style={{ gap: spacing.xs }}>
          <AppText variant="caption" tone="tertiary">
            DESCRIPTION
          </AppText>
          <TextInput
            value={description}
            onChangeText={setDescription}
            placeholder="What the role is, who should apply, how brothers can reach you"
            placeholderTextColor={palette.textTertiary}
            multiline
            style={[inputStyle, { minHeight: 120, textAlignVertical: "top" }]}
          />
        </View>

        <View style={{ gap: spacing.xs }}>
          <AppText variant="caption" tone="tertiary">
            APPLY LINK (OPTIONAL)
          </AppText>
          <TextInput
            value={applyUrl}
            onChangeText={setApplyUrl}
            placeholder="https://"
            placeholderTextColor={palette.textTertiary}
            autoCapitalize="none"
            keyboardType="url"
            style={inputStyle}
          />
        </View>

        {error ? (
          <AppText tone="danger" variant="caption">
            {error}
          </AppText>
        ) : null}

        <Button
          label={submitting ? "Posting…" : "Publish to job board"}
          onPress={() => void onSubmit()}
          disabled={!canSubmit}
        />
        <Button label="Cancel" variant="ghost" onPress={() => router.back()} />
      </View>
    </Screen>
  );
}
