/** Lineage API: families, big/little edges, full tree fetch — routers/lineage.py. */

import { request } from "./client";

export interface FamilyCreate {
  name: string;
  color?: string;
}

export interface FamilyOut {
  id: string;
  chapter_id: string;
  name: string;
  color: string;
}

export interface LineageEdgeCreate {
  big_user_id: string;
  little_user_id: string;
  family_id?: string | null;
  pledge_class?: string | null;
  /** Atomically replace the little's existing edge instead of 409ing (c79's
   * reassignment path). The new edge starts unconfirmed — the little confirms
   * the NEW big; the old confirmation never carries over. */
  replace_existing?: boolean;
}

export interface LineageEdgeOut {
  id: string;
  chapter_id: string;
  big_user_id: string;
  little_user_id: string;
  family_id: string | null;
  pledge_class: string | null;
  confirmed_by_little: boolean;
  created_by: string;
  created_at: string;
}

/** One member node for the tree render — user info + family placement. Ghosts are placeholders. */
export interface LineageNodeOut {
  user_id: string;
  display_name: string;
  avatar_url: string | null;
  is_ghost: boolean;
  family_id: string | null;
  pledge_class: string | null;
  /** Generation depth from a root (0 = no big / family head). */
  depth?: number;
}

/** Full adjacency for a chapter: nodes + edges + families (SPEC §4). */
export interface LineageTreeOut {
  nodes: LineageNodeOut[];
  edges: LineageEdgeOut[];
  families: FamilyOut[];
}

export async function getLineage(chapterId: string): Promise<LineageTreeOut> {
  return request<LineageTreeOut>(`/chapters/${chapterId}/lineage`);
}

export async function createFamily(chapterId: string, body: FamilyCreate): Promise<FamilyOut> {
  return request<FamilyOut>(`/chapters/${chapterId}/lineage/families`, { method: "POST", body });
}

export async function createEdge(
  chapterId: string,
  body: LineageEdgeCreate,
): Promise<LineageEdgeOut> {
  return request<LineageEdgeOut>(`/chapters/${chapterId}/lineage/edges`, { method: "POST", body });
}

/** Remove an edge outright (pure unpair, e-board only). Reassignment should use
 * createEdge with replace_existing instead — one atomic call, never two. */
export async function deleteEdge(chapterId: string, edgeId: string): Promise<void> {
  await request<void>(`/chapters/${chapterId}/lineage/edges/${edgeId}`, { method: "DELETE" });
}

/** Little confirms their big — flips confirmed_by_little. */
export async function confirmEdge(chapterId: string, edgeId: string): Promise<LineageEdgeOut> {
  return request<LineageEdgeOut>(`/chapters/${chapterId}/lineage/edges/${edgeId}/confirm`, {
    method: "POST",
  });
}
