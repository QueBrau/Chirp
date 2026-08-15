/** Alumni hub: chapter directory (work + contact) and job board. */

import { useCallback, useState } from "react";
import { Linking, Pressable, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";

import { getAlumniDirectory, listJobs, type AlumniProfileOut, type JobPostOut } from "@/api/alumni";
import { AppText, Avatar, Badge, Button, Card, EmptyState, Screen } from "@/components";
import { radii, spacing, useTheme } from "@/theme";

type Segment = "directory" | "jobs";

function AlumniCard({ profile }: { profile: AlumniProfileOut }) {
  const name = profile.display_name ?? "Alumni";
  const workLine = [profile.title, profile.company].filter(Boolean).join(" · ");
  const placeLine = [profile.location, profile.industry].filter(Boolean).join(" · ");

  return (
    <Card>
      <View style={{ gap: spacing.md }}>
        <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
          <Avatar name={name} size={44} />
          <View style={{ flex: 1, gap: 2 }}>
            <AppText style={{ fontWeight: "600" }}>{name}</AppText>
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
      </View>
    </Card>
  );
}

function JobCard({ job }: { job: JobPostOut }) {
  // posted_by is a real user uuid — posted_by_name comes from the backend's
  // join onto it (GET /jobs). Null when the join didn't run (POST /jobs) or
  // the poster row is gone; "Alumni" is the fallback, never a mock lookup.
  const posterName = job.posted_by_name ?? "Alumni";

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
            <AppText tone="accent" style={{ fontWeight: "600" }}>
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
  const [segment, setSegment] = useState<Segment>("directory");
  const [alumni, setAlumni] = useState<AlumniProfileOut[] | null>(null);
  const [jobs, setJobs] = useState<JobPostOut[] | null>(null);

  useFocusEffect(
    useCallback(() => {
      void getAlumniDirectory().then(setAlumni);
      void listJobs().then(setJobs);
    }, []),
  );

  return (
    <Screen title="Alumni" subtitle="Directory and chapter job board">
      <View style={{ gap: spacing.lg }}>
        <View
          style={{
            flexDirection: "row",
            gap: spacing.sm,
            padding: spacing.xs,
            borderRadius: radii.lg,
            backgroundColor: palette.surface,
            borderWidth: 1,
            borderColor: palette.border,
          }}
        >
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
                onPress={() => setSegment(key)}
                style={{
                  flex: 1,
                  paddingVertical: spacing.sm,
                  borderRadius: radii.md,
                  backgroundColor: active ? palette.accentMuted : "transparent",
                  alignItems: "center",
                }}
              >
                <AppText
                  variant="caption"
                  style={{ fontWeight: "600", color: active ? palette.accent : palette.textSecondary }}
                >
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
              message="When brothers graduate and fill out a profile, they'll show up here."
            />
          ) : (
            <View style={{ gap: spacing.md }}>
              {alumni.map((profile) => (
                <AlumniCard key={profile.user_id} profile={profile} />
              ))}
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
              jobs.map((job) => <JobCard key={job.id} job={job} />)
            )}
          </View>
        )}
      </View>
    </Screen>
  );
}
