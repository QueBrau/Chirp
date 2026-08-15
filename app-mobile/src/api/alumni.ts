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
  /** Joined from users (GET /jobs only — POST /jobs returns null here). */
  posted_by_name: string | null;
}

export async function getMyAlumniProfile(): Promise<AlumniProfileOut> {
  return request<AlumniProfileOut>("/alumni/profile");
}

export async function updateAlumniProfile(body: AlumniProfileUpdate): Promise<AlumniProfileOut> {
  return request<AlumniProfileOut>("/alumni/profile", { method: "PUT", body });
}

export async function getAlumniDirectory(): Promise<AlumniProfileOut[]> {
  return request<AlumniProfileOut[]>("/alumni/directory");
}

export async function listJobs(): Promise<JobPostOut[]> {
  return request<JobPostOut[]>("/jobs");
}

export async function createJob(body: JobPostCreate): Promise<JobPostOut> {
  return request<JobPostOut>("/jobs", { method: "POST", body });
}

export async function deleteJob(jobId: string): Promise<void> {
  return request<void>(`/jobs/${jobId}`, { method: "DELETE" });
}
