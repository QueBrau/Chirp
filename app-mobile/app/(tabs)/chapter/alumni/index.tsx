/** Alumni hub: chapter directory (work + contact) and job board. */

import { Feather } from "@expo/vector-icons";
import { useCallback, useState } from "react";
import { Linking, Pressable, StyleSheet, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";

import { getAlumniDirectory, listJobs, type AlumniProfileOut, type JobPostOut } from "@/api/alumni";
import { getRoleTerms, type RoleName } from "@/api/chapters";
import { highestOfficeLabel } from "@/lib/roleTerms";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import { AppText, Avatar, Badge, Button, Card, Chip, EmptyState, Screen } from "@/components";
import { radii, spacing, useTheme } from "@/theme";

/** One page of directory entries, and of postings. The server caps both at 200 (c258). */
const PAGE_SIZE = 50;

type Segment = "directory" | "jobs";

/**
 * Highest office held, fetched lazily (board card c181).
 *
 * N+1 GUARD: the directory can list many alumni, so this must never fire
 * GET .../role-terms for every row on render. It fires once, on tap of the
 * "Role history" affordance below — a real detail interaction the card gets
 * specifically so this enrichment has somewhere on-demand to live — and the
 * result is cached in local state so re-collapsing/re-expanding the same
 * card never re-fetches. `chapterId === null` (own-chapter still resolving,
 * or none) hides the affordance entirely rather than fetching against a
 * missing id.
 */
function AlumniCard({
  profile,
  chapterId,
  eboard,
}: {
  profile: AlumniProfileOut;
  chapterId: string | null;
  eboard: RoleName[];
}) {
  const palette = useTheme();
  const name = profile.display_name ?? "Alumni";
  const workLine = [profile.title, profile.company].filter(Boolean).join(" · ");
  const placeLine = [profile.location, profile.industry].filter(Boolean).join(" · ");

  const [expanded, setExpanded] = useState(false);
  // undefined = not fetched yet; null = fetched, no real e-board term (never an
  // invented claim); string = the "President, 2026" label.
  const [officeLabel, setOfficeLabel] = useState<string | null | undefined>(undefined);

  const toggleRoleHistory = () => {
    if (chapterId === null) return;
    setExpanded((current) => !current);
    if (officeLabel === undefined) {
      void getRoleTerms(chapterId, profile.user_id)
        .then((terms) => setOfficeLabel(highestOfficeLabel(terms, eboard)))
        .catch(() => setOfficeLabel(null));
    }
  };

  return (
    <Card>
      <View style={{ gap: spacing.md }}>
        <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
          <Avatar name={name} size={44} />
          <View style={{ flex: 1, gap: 2 }}>
            <AppText variant="headline">{name}</AppText>
            {profile.grad_year ? (
              <AppText variant="caption" tone="tertiary">
                Class of {profile.grad_year}
              </AppText>
            ) : null}
          </View>
          {profile.open_to_mentoring ? <Badge label="Mentoring" tone="accent" /> : null}
        </View>

        {workLine ? <AppText>{workLine}</AppText> : null}
        {placeLine ? (
          <AppText variant="caption" tone="secondary">
            {placeLine}
          </AppText>
        ) : null}

        <View style={{ gap: spacing.xs }}>
          {profile.email ? (
            <Pressable onPress={() => void Linking.openURL(`mailto:${profile.email}`)}>
              <AppText variant="caption" tone="accent">
                {profile.email}
              </AppText>
            </Pressable>
          ) : null}
          {profile.linkedin_url ? (
            <Pressable onPress={() => void Linking.openURL(profile.linkedin_url!)}>
              <AppText variant="caption" tone="accent">
                LinkedIn
              </AppText>
            </Pressable>
          ) : null}
        </View>

        {chapterId !== null ? (
          <View
            style={{
              gap: spacing.sm,
              paddingTop: spacing.sm,
              borderTopWidth: StyleSheet.hairlineWidth,
              borderTopColor: palette.border,
            }}
          >
            <Pressable
              accessibilityRole="button"
              accessibilityLabel={`${expanded ? "Hide" : "Show"} ${name}'s role history`}
              onPress={toggleRoleHistory}
              style={({ pressed }) => ({
                flexDirection: "row",
                alignItems: "center",
                gap: spacing.xs,
                opacity: pressed ? 0.7 : 1,
              })}
            >
              <Feather
                name={expanded ? "chevron-up" : "chevron-down"}
                size={14}
                color={palette.inkFaint}
              />
              <AppText variant="caption" tone="tertiary">
                Role history
              </AppText>
            </Pressable>
            {expanded ? (
              officeLabel === undefined ? (
                <AppText variant="caption" tone="tertiary">
                  Checking role history...
                </AppText>
              ) : officeLabel === null ? (
                <AppText variant="caption" tone="tertiary">
                  No officer history on record
                </AppText>
              ) : (
                <Chip label={officeLabel} variant="accent" />
              )
            ) : null}
          </View>
        ) : null}
      </View>
    </Card>
  );
}

function JobCard({ job }: { job: JobPostOut }) {
  // Server-joined name (GET /jobs). This used to be mockUserById(job.posted_by),
  // which resolved a REAL uuid against the mock table, never matched, and so
  // rendered "Alumni" for every job ever posted.
  const posterName = job.posted_by_name ?? "A member";

  return (
    <Card>
      <View style={{ gap: spacing.sm }}>
        <View style={{ flexDirection: "row", alignItems: "flex-start", gap: spacing.sm }}>
          <View style={{ flex: 1, gap: 2 }}>
            <AppText variant="title">{job.title}</AppText>
            <AppText tone="secondary">
              {job.company}
              {job.location ? ` · ${job.location}` : ""}
            </AppText>
          </View>
          {job.chapter_id ? <Badge label="Chapter" tone="neutral" /> : <Badge label="Network" tone="accent" />}
        </View>
        <AppText variant="caption" tone="secondary">
          Posted by {posterName}
        </AppText>
        <AppText>{job.description}</AppText>
        {job.apply_url ? (
          <Pressable onPress={() => void Linking.openURL(job.apply_url!)}>
            <AppText variant="bodyBold" tone="accent">
              Apply / learn more
            </AppText>
          </Pressable>
        ) : null}
      </View>
    </Card>
  );
}

export default function AlumniScreen() {
  const palette = useTheme();
  const router = useRouter();
  // Own chapter (single-org world, OwnChapterProvider) is also the shared
  // chapter behind GET /alumni/directory's "shares a chapter with the caller"
  // rule (routers/alumni.py) — it's the id role-terms lookups need per row.
  const { membership, roleMeta } = useOwnChapter();
  const chapterId = membership?.chapter_id ?? null;
  const eboard = roleMeta?.eboard ?? [];
  const [segment, setSegment] = useState<Segment>("directory");
  const [alumni, setAlumni] = useState<AlumniProfileOut[] | null>(null);
  const [jobs, setJobs] = useState<JobPostOut[] | null>(null);
  /** A full page means older rows exist behind it (c258). */
  const [hasOlderAlumni, setHasOlderAlumni] = useState(false);
  const [hasOlderJobs, setHasOlderJobs] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);

  /** Append the next page of whichever list asked. Both lists are render-only - the
   * screen's only reads of them are empty-state checks, which stay correct when paged. */
  const loadOlderAlumni = async () => {
    const oldest = (alumni ?? [])[(alumni ?? []).length - 1];
    if (oldest === undefined || loadingOlder) return;
    setLoadingOlder(true);
    try {
      const older = await getAlumniDirectory({ beforeId: oldest.user_id, limit: PAGE_SIZE });
      setHasOlderAlumni(older.length === PAGE_SIZE);
      setAlumni((current) => [...(current ?? []), ...older]);
    } finally {
      setLoadingOlder(false);
    }
  };

  const loadOlderJobs = async () => {
    const oldest = (jobs ?? [])[(jobs ?? []).length - 1];
    if (oldest === undefined || loadingOlder) return;
    setLoadingOlder(true);
    try {
      const older = await listJobs({
        before: oldest.created_at,
        beforeId: oldest.id,
        limit: PAGE_SIZE,
      });
      setHasOlderJobs(older.length === PAGE_SIZE);
      setJobs((current) => [...(current ?? []), ...older]);
    } finally {
      setLoadingOlder(false);
    }
  };

  useFocusEffect(
    useCallback(() => {
      void getAlumniDirectory({ limit: PAGE_SIZE }).then((page) => {
        setAlumni(page);
        setHasOlderAlumni(page.length === PAGE_SIZE);
      });
      void listJobs({ limit: PAGE_SIZE }).then((page) => {
        setJobs(page);
        setHasOlderJobs(page.length === PAGE_SIZE);
      });
    }, []),
  );

  return (
    <Screen title="Alumni" subtitle="Directory and chapter job board">
      <View style={{ gap: spacing.lg }}>
        <View style={{ flexDirection: "row", gap: spacing.sm }}>
          {(
            [
              ["directory", "Directory"],
              ["jobs", "Jobs"],
            ] as const
          ).map(([key, label]) => {
            const active = segment === key;
            return (
              <Pressable
                key={key}
                accessibilityRole="button"
                accessibilityState={{ selected: active }}
                onPress={() => setSegment(key)}
                style={({ pressed }) => ({
                  flex: 1,
                  alignItems: "center",
                  paddingVertical: spacing.sm,
                  borderRadius: radii.pill,
                  backgroundColor: active ? palette.accent : palette.surfaceAlt,
                  opacity: pressed ? 0.85 : 1,
                })}
              >
                <AppText variant="bodyBold" tone={active ? "onAccent" : "secondary"}>
                  {label}
                </AppText>
              </Pressable>
            );
          })}
        </View>

        {segment === "directory" ? (
          alumni === null ? (
            <EmptyState title="Loading alumni..." />
          ) : alumni.length === 0 ? (
            <EmptyState
              title="No alumni yet"
              message="When members graduate and fill out a profile, they'll show up here."
            />
          ) : (
            <View style={{ gap: spacing.md }}>
              {alumni.map((profile) => (
                <AlumniCard
                  key={profile.user_id}
                  profile={profile}
                  chapterId={chapterId}
                  eboard={eboard}
                />
              ))}
              {hasOlderAlumni ? (
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel="Load more alumni"
                  accessibilityState={{ disabled: loadingOlder, busy: loadingOlder }}
                  disabled={loadingOlder}
                  onPress={() => void loadOlderAlumni()}
                  style={({ pressed }) => ({
                    alignSelf: "center",
                    paddingVertical: spacing.sm,
                    paddingHorizontal: spacing.lg,
                    borderRadius: radii.pill,
                    backgroundColor: palette.surfaceAlt,
                    opacity: loadingOlder ? 0.6 : pressed ? 0.8 : 1,
                  })}
                >
                  <AppText variant="micro" tone="secondary">
                    {loadingOlder ? "Loading…" : "Load more alumni"}
                  </AppText>
                </Pressable>
              ) : null}
            </View>
          )
        ) : (
          <View style={{ gap: spacing.md }}>
            <Button
              label="Post a job"
              onPress={() => router.push("/chapter/alumni/post-job")}
            />
            {jobs === null ? (
              <EmptyState title="Loading jobs..." />
            ) : jobs.length === 0 ? (
              <EmptyState
                title="No open roles"
                message="Alumni and e-board can post openings for the chapter."
              />
            ) : (
              <>
                {jobs.map((job) => (
                  <JobCard key={job.id} job={job} />
                ))}
                {hasOlderJobs ? (
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel="Load older postings"
                  accessibilityState={{ disabled: loadingOlder, busy: loadingOlder }}
                  disabled={loadingOlder}
                  onPress={() => void loadOlderJobs()}
                  style={({ pressed }) => ({
                    alignSelf: "center",
                    paddingVertical: spacing.sm,
                    paddingHorizontal: spacing.lg,
                    borderRadius: radii.pill,
                    backgroundColor: palette.surfaceAlt,
                    opacity: loadingOlder ? 0.6 : pressed ? 0.8 : 1,
                  })}
                >
                  <AppText variant="micro" tone="secondary">
                    {loadingOlder ? "Loading…" : "Load older postings"}
                  </AppText>
                </Pressable>
              ) : null}
              </>
            )}
          </View>
        )}
      </View>
    </Screen>
  );
}
