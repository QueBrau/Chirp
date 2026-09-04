/**
 * New conversation picker (board c273): multi-select over the caller's own
 * chapter roster, active members only, self excluded. 1 pick -> kind "dm",
 * 2+ -> kind "group" with an optional title. POSTs createConversation() —
 * its first real call site; src/api/messages.ts has carried it, uncalled,
 * since it was added.
 *
 * No FAB: DESIGN.md §7 is explicit ("One FAB, Home only") and there is no
 * header-action slot on the shared Screen component to add one to. The entry
 * point instead lives in messages/index.tsx as a header-adjacent ghost pill,
 * the same shape as profile/index.tsx's local EditLayoutToggle.
 *
 * Honest-limit rule: creating a conversation here does NOT unlock sending —
 * src/crypto/signal.ts is still a typed stub (every export throws
 * TODO(milestone-3)), so sendMessage() has nothing that can call it. The
 * thread screen (app/(tabs)/messages/[id].tsx) already discloses this at the
 * composer ("Sending unlocks with E2EE (milestone 4)"); this screen repeats
 * that EXACT line rather than inventing a second, differently-worded claim
 * about encryption or sending (this repo has shipped that mistake twice).
 */

import { useRouter } from "expo-router";
import { useEffect, useRef, useState } from "react";
import { Pressable, TextInput, View } from "react-native";

import { Feather } from "@expo/vector-icons";

import { listMembers, type MemberOut } from "@/api/chapters";
import { ApiError } from "@/api/client";
import {
  createConversation,
  searchUsers,
  type ConversationKind,
  type UserSearchResult,
} from "@/api/messages";
import { useSession } from "@/auth";
import { showAlert } from "@/lib/alert";
import { roleLabel } from "@/lib/roleTerms";
import { AppText, Button, Card, EmptyState, GradientAvatar, ListRow, Screen } from "@/components";
import { inputField, radii, spacing, typography, useTheme } from "@/theme";

/**
 * Off-chapter people search (board c322). Server minimum is
 * routers/messages.py:MIN_SEARCH_QUERY_LENGTH — kept in sync here rather than
 * imported, same as every other client-side mirror of a server constant in this
 * app (there is no shared-constants build step between the two runtimes).
 */
const MIN_SEARCH_QUERY_LENGTH = 2;
/** Debounce so a keystroke is not a request — collapses a burst of typing into one call. */
const SEARCH_DEBOUNCE_MS = 350;

/** Someone the picker can show and select, whichever list they came from. */
interface PickableUser {
  id: string;
  displayName: string;
  avatarUrl: string | null;
  /** Roster rows only — search results carry no role (server returns name/avatar only). */
  subtitle?: string;
}

type SearchLoadState = "idle" | "loading" | "loaded" | "error";

/** Human sentence for a failed GET /users/search — same house style as createConversationErrorMessage. */
function searchUsersErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 429) {
    return "Too many searches at once. Wait a moment and try again.";
  }
  return "Couldn't search right now.";
}

/**
 * Human sentences for the codes routers/messages.py:126 can 403/404 with.
 * Never surface a raw server code like "recipient_not_reachable" verbatim
 * (c164 nit) — apiErrorMessage()/showApiError() would do exactly that for an
 * unmapped ApiError, so known codes are mapped here first and everything
 * else falls back to a generic line.
 */
function createConversationErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.detail) {
      case "not_a_member":
        return "You need to be an active member of this chapter to start this conversation.";
      case "recipient_not_reachable":
        return "Couldn't start that conversation with everyone you picked.";
      case "user_not_found":
        return "One of the people you picked couldn't be found. Try again.";
      default:
        break;
    }
  }
  return "Something went wrong. Try again.";
}

/** Selection state for a roster row: filled accent check when picked, an outline ring otherwise. */
function SelectionMark({ selected }: { selected: boolean }) {
  const palette = useTheme();
  return (
    <View
      style={{
        width: 24,
        height: 24,
        borderRadius: radii.pill,
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: selected ? palette.accent : "transparent",
        borderWidth: selected ? 0 : 2,
        borderColor: palette.border,
      }}
    >
      {selected ? <Feather name="check" size={14} color={palette.onAccent} /> : null}
    </View>
  );
}

export default function NewConversationScreen() {
  const router = useRouter();
  const palette = useTheme();
  const { status: sessionStatus, user, memberships } = useSession();
  // Single-org world (the same derivation OwnChapterProvider itself does):
  // memberships[0] is the caller's only chapter. That provider isn't in
  // scope here — it only wraps the chapter/* stack (chapter/_layout.tsx) —
  // so this reads the session's own embedded memberships directly instead.
  const membership = memberships[0] ?? null;
  const chapterId = membership?.chapter_id ?? null;

  const [members, setMembers] = useState<MemberOut[] | null>(null);
  // A Map, not a Set of ids: a person picked from search results can leave the
  // visible list the moment the query changes (cleared, or edited to something
  // else), and the selection must survive that so submit() still has a name to
  // show and the chip strip below still has one to render. The roster case needs
  // nothing more than the id, but keying on it here means one shape serves both.
  const [selectedUsers, setSelectedUsers] = useState<Map<string, PickableUser>>(new Map());
  const [groupTitle, setGroupTitle] = useState("");
  const [submitting, setSubmitting] = useState(false);
  /** The roster fetch failed. Distinct from a chapter with nobody to add (c317). */
  const [loadFailed, setLoadFailed] = useState(false);
  // A ref, not the state above: two taps in the same frame both run against
  // the SAME render's closure, where `submitting` is still false, so the state
  // check cannot see the first tap. The ref mutates synchronously and can.
  const submittingRef = useRef(false);

  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<UserSearchResult[] | null>(null);
  const [searchState, setSearchState] = useState<SearchLoadState>("idle");
  const [searchError, setSearchError] = useState<string | null>(null);
  // Bumped on every query change and compared against in the debounced fetch below,
  // so a slow response to an EARLIER query can never overwrite a newer one's result -
  // the same shape of race the submittingRef comment above guards against, just for
  // network order instead of tap order.
  const searchSeq = useRef(0);

  useEffect(() => {
    if (chapterId === null) {
      setMembers(null);
      return;
    }
    // NOT `.catch(() => setMembers([]))` (c317), and note the justification that
    // used to sit here — "matches the repo pattern elsewhere in this stack" — was
    // the identical sentence c299 removed from chapter/members.tsx. An empty roster
    // is the server saying this chapter has nobody to add; a failed request says
    // nothing at all, and rendering both as "No one to add yet" tells a member of
    // an eight-person chapter they have no one to talk to.
    setLoadFailed(false);
    listMembers(chapterId)
      .then(setMembers)
      .catch(() => setLoadFailed(true));
  }, [chapterId]);

  const trimmedQuery = query.trim();
  const isBelowMinLength = trimmedQuery.length > 0 && trimmedQuery.length < MIN_SEARCH_QUERY_LENGTH;
  const isSearching = trimmedQuery.length >= MIN_SEARCH_QUERY_LENGTH;

  useEffect(() => {
    if (!isSearching) {
      // Covers both the empty box and the below-minimum dead zone: neither is a
      // request, and clearing any stale result here is what lets the roster (or
      // the "keep typing" hint) show instead of a leftover result list.
      searchSeq.current += 1;
      setSearchState("idle");
      setSearchResults(null);
      setSearchError(null);
      return;
    }
    const seq = ++searchSeq.current;
    setSearchState("loading");
    const timer = setTimeout(() => {
      searchUsers(trimmedQuery)
        .then((results) => {
          if (searchSeq.current !== seq) return; // a newer query already superseded this one
          setSearchResults(results);
          setSearchState("loaded");
        })
        .catch((error) => {
          if (searchSeq.current !== seq) return;
          setSearchError(searchUsersErrorMessage(error));
          setSearchState("error");
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [isSearching, trimmedQuery]);

  // Session-status gating (same rule as chapter/members.tsx): a real member's
  // roster must never flash "no one to add" while the session or roster are
  // still resolving.
  // `!loadFailed &&` matters: on failure `members` stays null, and without this the
  // screen would sit on "Loading roster..." forever instead of saying what happened.
  const loading =
    !loadFailed && (sessionStatus === "loading" || (membership !== null && members === null));

  const roster = (members ?? []).filter(
    (member) => member.status === "active" && member.user_id !== user?.id,
  );

  const toggle = (person: PickableUser) => {
    setSelectedUsers((current) => {
      const next = new Map(current);
      if (next.has(person.id)) next.delete(person.id);
      else next.set(person.id, person);
      return next;
    });
  };

  const kind: ConversationKind = selectedUsers.size >= 2 ? "group" : "dm";

  // The server's chapter-scoped path (body.chapter_id set) requires EVERY named
  // person to be an active member of THAT chapter — it never runs the off-chapter
  // reachability rule at all (routers/messages.py create_conversation branches on
  // chapter_id being None). So a selection reaching outside the roster (anyone
  // found via search, board c322) can only go through with chapter_id omitted;
  // sending the caller's own chapterId for it would 403 `not_a_member` on someone
  // the server would otherwise happily let the caller reach. A pure roster pick
  // keeps going through the chapter path unchanged, exactly like c273 shipped it.
  const selectionIsEntirelyRoster =
    chapterId !== null &&
    [...selectedUsers.keys()].every((id) => roster.some((member) => member.user_id === id));

  const submit = async () => {
    // Re-checked here, not just via the Button's disabled prop: a fast
    // double-tap queues the second onPress before the first setSubmitting(true)
    // has re-rendered, so both would see `disabled={false}` AND a stale
    // `submitting === false`. Only the synchronous ref stops the second POST.
    if (selectedUsers.size === 0 || submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    try {
      const conversation = await createConversation({
        chapter_id: selectionIsEntirelyRoster ? chapterId : null,
        kind,
        title: kind === "group" && groupTitle.trim().length > 0 ? groupTitle.trim() : null,
        member_user_ids: [...selectedUsers.keys()],
      });
      // replace(), not push(): back from the new thread should return to the
      // conversation list, not to this picker.
      router.replace(`/messages/${conversation.id}`);
    } catch (error) {
      showAlert("Couldn't start that conversation", createConversationErrorMessage(error));
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  return (
    <Screen
      title="New conversation"
      subtitle={loading ? undefined : "Search or pick someone to start with"}
    >
      {loading ? (
        <EmptyState title="Loading roster..." />
      ) : loadFailed ? (
        // Ahead of the "No one to add yet" branch below, because that branch is what
        // the failure used to fall through into. The state alone does not fix this
        // screen — without a branch that reads it, the roster still renders the empty
        // copy and the fix is invisible. Caught by the live pass, not by tsc.
        <EmptyState
          title="Couldn't load the roster"
          message="Check your connection and try again. This isn't a statement that your chapter has nobody to message."
        />
      ) : (
        <View style={{ gap: spacing.lg }}>
          {/* Default listing is the chapter roster; typing here searches the wider
              reachable set (board c322) — a chapter mate OR, while campus-verified,
              anyone on the caller's campus. Shown regardless of whether the caller
              has a chapter at all, since the campus half of that rule needs no
              chapter membership to apply. */}
          <View
            style={{
              borderRadius: radii.input,
              borderWidth: 1,
              borderColor: palette.border,
              overflow: "hidden",
            }}
          >
            <TextInput
              value={query}
              onChangeText={setQuery}
              placeholder="Search for someone to message"
              placeholderTextColor={palette.inkFaint}
              autoCapitalize="words"
              autoCorrect={false}
              style={{
                ...typography.body,
                color: palette.ink,
                backgroundColor: palette.surfaceAlt,
                paddingHorizontal: spacing.lg,
                paddingVertical: spacing.md,
              }}
            />
          </View>

          {selectedUsers.size > 0 ? (
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
              {[...selectedUsers.values()].map((person) => (
                <Pressable
                  key={person.id}
                  accessibilityRole="button"
                  accessibilityLabel={`Remove ${person.displayName}`}
                  onPress={() => toggle(person)}
                  style={({ pressed }) => ({
                    flexDirection: "row",
                    alignItems: "center",
                    gap: spacing.xs,
                    paddingHorizontal: spacing.md,
                    paddingVertical: spacing.xs,
                    borderRadius: radii.pill,
                    backgroundColor: palette.accentSoft,
                    opacity: pressed ? 0.8 : 1,
                  })}
                >
                  <AppText variant="caption" style={{ color: palette.accent }}>
                    {person.displayName}
                  </AppText>
                  <Feather name="x" size={12} color={palette.accent} />
                </Pressable>
              ))}
            </View>
          ) : null}

          {isSearching ? (
            searchState === "loading" ? (
              <EmptyState title="Searching..." />
            ) : searchState === "error" ? (
              <EmptyState title="Couldn't search right now" message={searchError ?? undefined} />
            ) : (searchResults ?? []).length === 0 ? (
              <EmptyState title="Nobody matches that" message="Try a different name." />
            ) : (
              <Card>
                {(searchResults ?? []).map((person, index) => {
                  const isSelected = selectedUsers.has(person.id);
                  return (
                    <ListRow
                      key={person.id}
                      title={person.display_name}
                      left={
                        <GradientAvatar
                          name={person.display_name}
                          size={40}
                          photoUrl={person.avatar_url ?? undefined}
                        />
                      }
                      right={<SelectionMark selected={isSelected} />}
                      divider={index < (searchResults ?? []).length - 1}
                      onPress={() =>
                        toggle({
                          id: person.id,
                          displayName: person.display_name,
                          avatarUrl: person.avatar_url,
                        })
                      }
                    />
                  );
                })}
              </Card>
            )
          ) : isBelowMinLength ? (
            <EmptyState
              title="Keep typing"
              message={`Search needs at least ${MIN_SEARCH_QUERY_LENGTH} characters.`}
            />
          ) : chapterId === null ? (
            <EmptyState
              title="Search for someone to message"
              message="You're not in a chapter yet, so search above for someone on your campus."
            />
          ) : roster.length === 0 ? (
            <EmptyState title="No one to add yet" message="Active chapter members will show up here." />
          ) : (
            <Card>
              {roster.map((member, index) => {
                const isSelected = selectedUsers.has(member.user_id);
                const label = member.display_name.length > 0 ? member.display_name : member.user_id;
                return (
                  <ListRow
                    key={member.id}
                    title={label}
                    subtitle={roleLabel(member.role)}
                    left={<GradientAvatar name={label} size={40} photoUrl={member.avatar_url} />}
                    right={<SelectionMark selected={isSelected} />}
                    divider={index < roster.length - 1}
                    onPress={() =>
                      toggle({
                        id: member.user_id,
                        displayName: label,
                        avatarUrl: member.avatar_url,
                        subtitle: roleLabel(member.role),
                      })
                    }
                  />
                );
              })}
            </Card>
          )}

          {kind === "group" ? (
            <Card>
              <AppText variant="micro" tone="secondary" style={{ marginBottom: spacing.xs }}>
                Group name (optional)
              </AppText>
              <TextInput
                value={groupTitle}
                onChangeText={setGroupTitle}
                placeholder="e.g. Pledge class 2027"
                placeholderTextColor={palette.inkFaint}
                style={inputField(palette)}
              />
            </Card>
          ) : null}

          <Button
            label={submitting ? "Starting..." : "Start conversation"}
            onPress={() => void submit()}
            disabled={selectedUsers.size === 0 || submitting}
          />

          <View
            style={{
              flexDirection: "row",
              alignItems: "center",
              justifyContent: "center",
              gap: spacing.xs,
            }}
          >
            <Feather name="lock" size={typography.caption.fontSize} color={palette.inkFaint} />
            <AppText variant="caption" tone="tertiary">
              Sending unlocks with E2EE (milestone 4)
            </AppText>
          </View>
        </View>
      )}
    </Screen>
  );
}
