/**
 * Profile → Alumni info editor — board card c423.
 *
 * Until this screen existed, `src/api/alumni.ts` exported `updateAlumniProfile`
 * and NOTHING in the app called it: the profile screen's "Add your company" and
 * "Add your class year" rows were plain `ListRow`s with no `onPress`, so they
 * invited an edit the app had no way to perform. c377 fixed how that section
 * distinguishes a successful-empty from a load failure; it did not give the rows
 * anywhere to go. This is that missing destination.
 *
 * FULL REPLACE, NOT A PATCH — the single most load-bearing fact about this
 * screen. `PUT /alumni/profile` (routers/alumni.py upsert_own_profile) assigns
 * EVERY column from the request body unconditionally, and every field on
 * `AlumniProfileUpdate` defaults to None/False. So a body carrying only
 * `company` does not leave the rest alone — it NULLs grad_year, title, industry,
 * location and linkedin_url and resets open_to_mentoring to false. A per-field
 * editor that sent one field per tap would therefore silently wipe the rest of
 * the profile. This screen edits the whole profile at once and always submits
 * every field, which is the only shape that is safe against that endpoint.
 *
 * NO PRIVILEGE IS INFERRED FROM `account_type` (c423's own acceptance bar). The
 * backend already works this way: the route reads `user.id` and writes only that
 * user's own row, with no account_type check anywhere. Profile's alumni section
 * is gated on `account_type === "alumni"` purely as a display choice, and this
 * screen adds no gate of its own. Reaching it for a non-alum would edit that
 * same self-owned row and grant nothing.
 *
 * A FAILED LOAD DOES NOT RENDER AN EMPTY FORM. That is the c319 defect in a new
 * place: a dropped request would present blank fields to an alum who filled them
 * in months ago, inviting them to retype everything, and then — because the save
 * is a full replace — letting them overwrite the real profile with the blanks.
 * Only the 404 that means "not created yet" opens an empty form; every other
 * failure gets an error state with a retry and no editable fields.
 */

import { useRouter } from "expo-router";
import { useCallback, useEffect, useState } from "react";
import { TextInput, View } from "react-native";

import {
  getMyAlumniProfile,
  updateAlumniProfile,
  type AlumniProfileOut,
} from "@/api/alumni";
import { ApiError } from "@/api/client";
import { AppText, Button, Card, EmptyState, ListRow, Screen } from "@/components";
import { inputField, spacing, useTheme } from "@/theme";

/**
 * Mirrors `validate_public_url` in backend/app/core/validation.py (c184: the
 * client opens this URL blind via Linking.openURL, a verified phishing /
 * intent-URI vector). Checked here as well as server-side so the rejection is a
 * sentence next to the field instead of a 422 whose FastAPI body is a LIST —
 * which `parseResponse` in api/client.ts cannot read into `ApiError.detail`,
 * leaving only the bare status text to show someone.
 */
const MAX_URL_LENGTH = 2048;

/**
 * Grad-year bounds are the CLIENT's, not a mirror of the server: the column is a
 * plain `int | None` with no Field bounds, so the backend would accept year 0 or
 * 99999 just as happily. Stated as a visible hint rather than enforced silently.
 */
const GRAD_YEAR_MIN = 1900;
/** Alumni and soon-to-be alumni both fill this in, so the future is allowed. */
const GRAD_YEAR_MAX_AHEAD = 10;

interface AlumniForm {
  company: string;
  title: string;
  industry: string;
  location: string;
  gradYear: string;
  linkedinUrl: string;
  openToMentoring: boolean;
}

const EMPTY_FORM: AlumniForm = {
  company: "",
  title: "",
  industry: "",
  location: "",
  gradYear: "",
  linkedinUrl: "",
  openToMentoring: false,
};

function formFromProfile(profile: AlumniProfileOut): AlumniForm {
  return {
    company: profile.company ?? "",
    title: profile.title ?? "",
    industry: profile.industry ?? "",
    location: profile.location ?? "",
    gradYear: profile.grad_year === null ? "" : String(profile.grad_year),
    linkedinUrl: profile.linkedin_url ?? "",
    openToMentoring: profile.open_to_mentoring,
  };
}

/**
 * Blank becomes null, never "". The columns are nullable and every read path in
 * the app tests them with `?? "Add your ..."`, which an empty string passes —
 * so storing "" would render a filled-in-looking row with nothing in it.
 */
function trimmedOrNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/** The field-level complaint for a value, or null when it is acceptable. */
function gradYearProblem(raw: string, thisYear: number): string | null {
  const trimmed = raw.trim();
  if (trimmed.length === 0) return null; // Clearing the year is allowed.
  if (!/^\d{4}$/.test(trimmed)) return "Class year should be four digits, like 2021.";
  const year = Number(trimmed);
  const max = thisYear + GRAD_YEAR_MAX_AHEAD;
  if (year < GRAD_YEAR_MIN || year > max) {
    return `Class year should be between ${GRAD_YEAR_MIN} and ${max}.`;
  }
  return null;
}

function linkedinProblem(raw: string): string | null {
  const trimmed = raw.trim();
  if (trimmed.length === 0) return null; // Clearing the link is allowed.
  if (trimmed.length > MAX_URL_LENGTH) return "That link is too long.";
  // Matches the server's urlsplit check: an allowed scheme AND a host. Parsed
  // rather than regex-matched so "https://" with nothing after it is caught.
  const match = /^(https?):\/\/([^/?#]+)/i.exec(trimmed);
  if (match === null) return "Links need to start with http:// or https://";
  return null;
}

export default function AlumniInfoScreen() {
  const router = useRouter();
  const palette = useTheme();

  const [form, setForm] = useState<AlumniForm>(EMPTY_FORM);
  const [loading, setLoading] = useState(true);
  /** A real failure to ANSWER, as opposed to the 404 that means "none yet". */
  const [loadFailed, setLoadFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const thisYear = new Date().getFullYear();

  const load = useCallback(async () => {
    setLoading(true);
    setLoadFailed(false);
    setError(null);
    try {
      setForm(formFromProfile(await getMyAlumniProfile()));
    } catch (err) {
      // c377's distinction, and this screen depends on it even more than the
      // profile screen does: 404 alumni_profile_not_found is the ANSWER "you
      // haven't made one yet", which is exactly the first-creation case this
      // editor exists to serve. Anything else is a failure to answer, and must
      // not open a blank form over a profile that may well have content.
      const notYetCreated =
        err instanceof ApiError &&
        err.status === 404 &&
        err.detail === "alumni_profile_not_found";
      if (notYetCreated) {
        setForm(EMPTY_FORM);
      } else {
        setLoadFailed(true);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const update = <K extends keyof AlumniForm>(key: K, value: AlumniForm[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const save = async () => {
    const problem = gradYearProblem(form.gradYear, thisYear) ?? linkedinProblem(form.linkedinUrl);
    if (problem !== null) {
      setError(problem);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const gradYear = form.gradYear.trim();
      // EVERY field, every time — see the full-replace note in the module
      // docstring. Dropping one here does not preserve it, it erases it.
      await updateAlumniProfile({
        grad_year: gradYear.length > 0 ? Number(gradYear) : null,
        company: trimmedOrNull(form.company),
        title: trimmedOrNull(form.title),
        industry: trimmedOrNull(form.industry),
        location: trimmedOrNull(form.location),
        linkedin_url: trimmedOrNull(form.linkedinUrl),
        open_to_mentoring: form.openToMentoring,
      });
      // The profile screen refetches on focus (c423), so going back is enough
      // to show the saved values — no param passing or shared store needed.
      router.back();
    } catch (err) {
      // `form` is deliberately untouched: a failed save must leave every
      // keystroke where the user left it, not hand back a cleared form.
      if (err instanceof ApiError && err.status === 422) {
        // FastAPI's validation body is a list, so ApiError.detail holds only
        // the status text here. Name the field the server validates instead of
        // showing that (api/client.ts parseResponse).
        setError("Check your LinkedIn link and class year, then try again.");
      } else if (err instanceof ApiError && err.status === 401) {
        setError("Your session expired. Sign in again and your changes will save.");
      } else {
        setError("Couldn't save your alumni info. Your changes are still here — try again.");
      }
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <Screen title="Alumni info" subtitle="Loading your profile…">
        <View />
      </Screen>
    );
  }

  if (loadFailed) {
    return (
      <Screen title="Alumni info">
        <View style={{ gap: spacing.xl }}>
          <EmptyState
            title="Couldn't load your alumni profile"
            message="Check your connection and try again. This isn't a statement that you haven't filled it in — editing is off until we can read what's already there."
            actionLabel="Try again"
            onAction={() => void load()}
          />
          <Button label="Back" variant="ghost" onPress={() => router.back()} />
        </View>
      </Screen>
    );
  }

  return (
    <Screen
      title="Alumni info"
      subtitle="Shown to people you share an org with, in the alumni directory."
    >
      <View style={{ gap: spacing.xl }}>
        <Card>
          <View style={{ gap: spacing.md }}>
            <AppText variant="headline">Company</AppText>
            <TextInput
              value={form.company}
              onChangeText={(value) => update("company", value)}
              placeholder="e.g. Red Hat"
              placeholderTextColor={palette.inkFaint}
              autoCorrect={false}
              style={inputField(palette)}
            />

            <AppText variant="headline">Role</AppText>
            <TextInput
              value={form.title}
              onChangeText={(value) => update("title", value)}
              placeholder="e.g. Software Engineer"
              placeholderTextColor={palette.inkFaint}
              autoCorrect={false}
              style={inputField(palette)}
            />

            <AppText variant="headline">Class year</AppText>
            <TextInput
              value={form.gradYear}
              onChangeText={(value) => update("gradYear", value)}
              placeholder="e.g. 2021"
              placeholderTextColor={palette.inkFaint}
              keyboardType="number-pad"
              maxLength={4}
              style={inputField(palette)}
            />

            <AppText variant="headline">Industry</AppText>
            <TextInput
              value={form.industry}
              onChangeText={(value) => update("industry", value)}
              placeholder="e.g. Software"
              placeholderTextColor={palette.inkFaint}
              autoCorrect={false}
              style={inputField(palette)}
            />

            <AppText variant="headline">Location</AppText>
            <TextInput
              value={form.location}
              onChangeText={(value) => update("location", value)}
              placeholder="e.g. Raleigh, NC"
              placeholderTextColor={palette.inkFaint}
              autoCorrect={false}
              style={inputField(palette)}
            />

            <AppText variant="headline">LinkedIn</AppText>
            <TextInput
              value={form.linkedinUrl}
              onChangeText={(value) => update("linkedinUrl", value)}
              placeholder="https://linkedin.com/in/you"
              placeholderTextColor={palette.inkFaint}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              style={inputField(palette)}
            />
            <AppText variant="caption" tone="tertiary">
              Leave anything blank to clear it. Everything here is optional.
            </AppText>
          </View>
        </Card>

        <Card>
          {/* A tappable row rather than a Switch: this app has no Switch
              anywhere, and the selectable-row-with-a-mark shape is the
              established idiom (profile/account-type.tsx). */}
          <ListRow
            title="Open to mentoring"
            subtitle={
              form.openToMentoring
                ? "Alumni in your orgs can see you're open to it."
                : "Not right now."
            }
            accessibilityLabel={`Open to mentoring, currently ${form.openToMentoring ? "on" : "off"}`}
            right={
              <AppText variant="headline" tone={form.openToMentoring ? "accent" : "tertiary"}>
                {form.openToMentoring ? "Yes" : "No"}
              </AppText>
            }
            onPress={() => update("openToMentoring", !form.openToMentoring)}
            divider={false}
          />
        </Card>

        {error !== null ? (
          <AppText variant="caption" tone="danger">
            {error}
          </AppText>
        ) : null}

        <Button
          label={saving ? "Saving…" : "Save"}
          disabled={saving}
          onPress={() => void save()}
        />
        {/* Cancel writes nothing — it only unwinds the push. */}
        <Button label="Cancel" variant="ghost" disabled={saving} onPress={() => router.back()} />
      </View>
    </Screen>
  );
}
