/**
 * Start your own org — board card c378. Reachable from the Orgs tab's "No orgs
 * yet" empty state (chapter/index.tsx's FindYourOrg), which before this card was
 * a dead end for anyone without an invite code: the only path in was typing a
 * code an e-board already handed them.
 *
 * Matches join-chapter.tsx's shape (Card + form + primary Button + a ghost way
 * out) rather than inventing a new one — same job, same screen family, and the
 * two are each other's sibling entry points into the Orgs tab.
 *
 * GATED ON CAMPUS VERIFICATION, because the server is (routers/chapters.py's
 * create_chapter, board c378): POST /chapters 403s campus_unverified for anyone
 * who has not proved a current .edu. Printing that raw code here would be
 * exactly the "bare error toast" c90's own docstring says this product rejects,
 * so this screen checks useCampusAccess() the same way CreateSheet does for
 * campus-audience posts, and shows a real "confirm your school first" card with
 * a button straight to /verify-campus instead of an org name field the server
 * would refuse.
 *
 * "loading" IS TREATED AS OFFERED (CreateSheet's own rule, same reasoning):
 * verified is the common case, and flashing a verify prompt at someone who
 * turns out to be verified is the wrong default. The submit-time catch below is
 * the safety net for that window and for a verification that lapses between
 * this screen mounting and the button being pressed.
 */

import { useRouter } from "expo-router";
import { useState } from "react";
import { TextInput, View } from "react-native";

import { createChapter } from "@/api/chapters";
import { ApiError } from "@/api/client";
import { useCampusAccess, useSession } from "@/auth";
import { AppText, Button, Card, Screen } from "@/components";
import { inputField, spacing, useTheme } from "@/theme";

export default function CreateChapterScreen() {
  const router = useRouter();
  const palette = useTheme();
  const { refresh } = useSession();
  const access = useCampusAccess();

  const [orgName, setOrgName] = useState("");
  const [chapterName, setChapterName] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set by the submit-time 403 (see the module docstring's "safety net"), not by
  // useCampusAccess directly — that keeps ONE code path deciding when the gate
  // card shows, whether the reason is "already known unverified" or "found out
  // just now".
  const [blockedOnVerification, setBlockedOnVerification] = useState(false);

  const lapsed = access === "lapsed";
  const needsVerification = blockedOnVerification || access === "unverified" || access === "lapsed";

  if (needsVerification) {
    return (
      <Screen
        title="Confirm your school first"
        subtitle="Founding an org is a campus-wide action, so it needs the same proof campus posts and Chirps do."
      >
        <View style={{ gap: spacing.xl }}>
          <Card>
            <AppText variant="body">
              {lapsed
                ? "It's been a year since you confirmed your .edu address. Verify again and you can start your org right after."
                : "Confirm your .edu address and you can start your org right after."}
            </AppText>
          </Card>
          <Button label="Verify your school" onPress={() => router.push("/verify-campus")} />
          <Button label="Not now" variant="ghost" onPress={() => router.back()} />
        </View>
      </Screen>
    );
  }

  const create = async () => {
    setCreating(true);
    setError(null);
    try {
      await createChapter({
        org_name: orgName.trim(),
        chapter_name: chapterName.trim().length > 0 ? chapterName.trim() : null,
      });
      // Same rule as join-chapter.tsx's settleSession: await so the Orgs tab we
      // return to already knows about the new membership, but nothing here
      // depends on the refresh itself succeeding — the chapter exists server-side
      // the moment createChapter resolves.
      await refresh().catch(() => undefined);
      router.replace("/chapter");
    } catch (err) {
      if (err instanceof ApiError && err.status === 403 && err.detail === "campus_unverified") {
        // The race useCampusAccess's own docstring warns about: access read "ok"
        // (or was still "loading") and the server disagreed. Switch to the same
        // gate card rather than printing the raw code.
        setBlockedOnVerification(true);
      } else if (err instanceof ApiError && err.status === 429) {
        setError("You've started a few orgs already. Give it a bit before trying again.");
      } else if (err instanceof ApiError && err.status === 400) {
        setError("That name doesn't work. Try a different one.");
      } else {
        setError("Something went wrong. Try again.");
      }
    } finally {
      setCreating(false);
    }
  };

  return (
    <Screen
      title="Start your own org"
      subtitle="Found a fraternity, sorority, or campus org at your school."
    >
      <View style={{ gap: spacing.xl }}>
        <Card>
          <View style={{ gap: spacing.md }}>
            <AppText variant="headline">Org name</AppText>
            <TextInput
              value={orgName}
              onChangeText={setOrgName}
              placeholder="e.g. Sigma Chi"
              placeholderTextColor={palette.inkFaint}
              autoCorrect={false}
              style={inputField(palette)}
            />
            <AppText variant="headline">Chapter name</AppText>
            <TextInput
              value={chapterName}
              onChangeText={setChapterName}
              placeholder="e.g. Epsilon Mu (optional)"
              placeholderTextColor={palette.inkFaint}
              autoCorrect={false}
              style={inputField(palette)}
            />
            <AppText variant="caption" tone="tertiary">
              You'll be its president. Campus moderation tools stay off until a
              platform admin approves the org.
            </AppText>
            {error !== null ? (
              <AppText variant="caption" tone="danger">
                {error}
              </AppText>
            ) : null}
            <Button
              label={creating ? "Creating..." : "Create org"}
              disabled={orgName.trim().length === 0 || creating}
              onPress={() => void create()}
            />
          </View>
        </Card>

        <Button label="Cancel" variant="ghost" onPress={() => router.back()} />
      </View>
    </Screen>
  );
}
