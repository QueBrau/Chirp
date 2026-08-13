/** Alumni API: profiles, directory, job board — routers/alumni.py. */

import { mocked, request, USE_MOCKS } from "./client";
import {
  MOCK_ALUMNI_PROFILES,
  MOCK_CURRENT_USER,
  MOCK_JOB_POSTS,
  newMockId,
  nowIso,
} from "../mocks/data";

/** Body for PUT /alumni/profile — upserts the caller's profile. */
export interface AlumniProfileUpdate {
  grad_year?: number | null;
  company?: string | null;
  title?: string | null;
  industry?: string | null;
  location?: string | null;
  linkedin_url?: string | null;
  open_to_mentoring?: boolean;
}

export interface AlumniProfileOut {
  user_id: string;
  grad_year: number | null;
  company: string | null;
  title: string | null;
  industry: string | null;
  location: string | null;
  linkedin_url: string | null;
  open_to_mentoring: boolean;
  /** Joined from users for directory views. */
  display_name: string | null;
  /** Joined from users — contact email for the directory. */
  email: string | null;
}

export interface JobPostCreate {
  chapter_id?: string | null; // null = network-wide
  title: string;
  company: string;
  location: string;
  description: string;
  apply_url?: string | null;
  expires_at?: string | null;
}

export interface JobPostOut {
  id: string;
  posted_by: string;
  chapter_id: string | null;
  title: string;
  company: string;
  location: string | null;
  description: string;
  apply_url: string | null;
  created_at: string;
  expires_at: string | null;
}

export async function getMyAlumniProfile(): Promise<AlumniProfileOut> {
  if (USE_MOCKS) {
    const own = MOCK_ALUMNI_PROFILES.find((p) => p.user_id === MOCK_CURRENT_USER.id);
    return mocked(
      own ?? {
        user_id: MOCK_CURRENT_USER.id,
        grad_year: null,
        company: null,
        title: null,
        industry: null,
        location: null,
        linkedin_url: null,
        open_to_mentoring: false,
        display_name: MOCK_CURRENT_USER.display_name,
        email: MOCK_CURRENT_USER.email,
      },
    );
  }
  return request<AlumniProfileOut>("/alumni/profile");
}

export async function updateAlumniProfile(body: AlumniProfileUpdate): Promise<AlumniProfileOut> {
  if (USE_MOCKS) {
    let profile = MOCK_ALUMNI_PROFILES.find((p) => p.user_id === MOCK_CURRENT_USER.id);
    if (!profile) {
      profile = {
        user_id: MOCK_CURRENT_USER.id,
        grad_year: null,
        company: null,
        title: null,
        industry: null,
        location: null,
        linkedin_url: null,
        open_to_mentoring: false,
        display_name: MOCK_CURRENT_USER.display_name,
        email: MOCK_CURRENT_USER.email,
      };
      MOCK_ALUMNI_PROFILES.push(profile);
    }
    Object.assign(profile, body);
    return mocked(profile);
  }
  return request<AlumniProfileOut>("/alumni/profile", { method: "PUT", body });
}

export async function getAlumniDirectory(): Promise<AlumniProfileOut[]> {
  if (USE_MOCKS) return mocked([...MOCK_ALUMNI_PROFILES]);
  return request<AlumniProfileOut[]>("/alumni/directory");
}

export async function listJobs(): Promise<JobPostOut[]> {
  if (USE_MOCKS) {
    return mocked(
      [...MOCK_JOB_POSTS].sort((a, b) => b.created_at.localeCompare(a.created_at)),
    );
  }
  return request<JobPostOut[]>("/jobs");
}

export async function createJob(body: JobPostCreate): Promise<JobPostOut> {
  if (USE_MOCKS) {
    const job: JobPostOut = {
      id: newMockId("job"),
      posted_by: MOCK_CURRENT_USER.id,
      chapter_id: body.chapter_id ?? null,
      title: body.title,
      company: body.company,
      location: body.location,
      description: body.description,
      apply_url: body.apply_url ?? null,
      created_at: nowIso(),
      expires_at: body.expires_at ?? null,
    };
    MOCK_JOB_POSTS.unshift(job);
    return mocked(job);
  }
  return request<JobPostOut>("/jobs", { method: "POST", body });
}

export async function deleteJob(jobId: string): Promise<void> {
  if (USE_MOCKS) {
    const index = MOCK_JOB_POSTS.findIndex((j) => j.id === jobId);
    if (index >= 0) MOCK_JOB_POSTS.splice(index, 1);
    return mocked(undefined);
  }
  return request<void>(`/jobs/${jobId}`, { method: "DELETE" });
}
