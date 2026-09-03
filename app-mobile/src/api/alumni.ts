/** Alumni API: profiles, directory, job board — routers/alumni.py. */

import { request } from "./client";

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
  /** Poster's display name, joined server-side by GET /jobs. Null on the
   *  create/delete responses, which do not run that join. */
  posted_by_name: string | null;
}

export async function getMyAlumniProfile(): Promise<AlumniProfileOut> {
  return request<AlumniProfileOut>("/alumni/profile");
}

export async function updateAlumniProfile(body: AlumniProfileUpdate): Promise<AlumniProfileOut> {
  return request<AlumniProfileOut>("/alumni/profile", { method: "PUT", body });
}

/** One page of the directory. SINGLE cursor field on purpose: alumni_profiles has no
 * created_at - a profile is not an event - and user_id is the primary key, so it is
 * unique and needs no tie-break companion (c258). */
export interface DirectoryPageQuery {
  beforeId?: string;
  limit?: number;
}

export async function getAlumniDirectory(
  options: DirectoryPageQuery = {},
): Promise<AlumniProfileOut[]> {
  return request<AlumniProfileOut[]>("/alumni/directory", {
    query: { before_id: options.beforeId, limit: options.limit },
  });
}

/** One page of postings, newest first (c258). */
export interface JobPageQuery {
  before?: string;
  beforeId?: string;
  limit?: number;
}

export async function listJobs(options: JobPageQuery = {}): Promise<JobPostOut[]> {
  // BOTH cursor halves or NEITHER - `before` alone cannot tie-break postings sharing a
  // timestamp and drops them at a page boundary.
  const paired = options.before !== undefined && options.beforeId !== undefined;
  return request<JobPostOut[]>("/jobs", {
    query: {
      before: paired ? options.before : undefined,
      before_id: paired ? options.beforeId : undefined,
      limit: options.limit,
    },
  });
}

export async function createJob(body: JobPostCreate): Promise<JobPostOut> {
  return request<JobPostOut>("/jobs", { method: "POST", body });
}

export async function deleteJob(jobId: string): Promise<void> {
  return request<void>(`/jobs/${jobId}`, { method: "DELETE" });
}
