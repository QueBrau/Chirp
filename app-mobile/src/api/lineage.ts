/** Lineage API: families, big/little edges, full tree fetch — routers/lineage.py. */

import { ApiError, mocked, request, USE_MOCKS } from "./client";
import { MOCK_CURRENT_USER, MOCK_LINEAGE_TREE, newMockId, nowIso } from "../mocks/data";

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
  if (USE_MOCKS) return mocked(MOCK_LINEAGE_TREE);
  return request<LineageTreeOut>(`/chapters/${chapterId}/lineage`);
}

export async function createFamily(chapterId: string, body: FamilyCreate): Promise<FamilyOut> {
  if (USE_MOCKS) {
    const family: FamilyOut = {
      id: newMockId("fam"),
      chapter_id: chapterId,
      name: body.name,
      color: body.color ?? "#6366f1",
    };
    MOCK_LINEAGE_TREE.families.push(family);
    return mocked(family);
  }
  return request<FamilyOut>(`/chapters/${chapterId}/lineage/families`, { method: "POST", body });
}

export async function createEdge(
  chapterId: string,
  body: LineageEdgeCreate,
): Promise<LineageEdgeOut> {
  if (USE_MOCKS) {
    if (body.big_user_id === body.little_user_id) {
      throw new ApiError(422, "big_and_little_identical");
    }
    // One big per little — mirrors UNIQUE (little_user_id, chapter_id).
    const existing = MOCK_LINEAGE_TREE.edges.find((e) => e.little_user_id === body.little_user_id);
    if (existing) throw new ApiError(409, "little_already_has_big");

    const edge: LineageEdgeOut = {
      id: newMockId("edge"),
      chapter_id: chapterId,
      big_user_id: body.big_user_id,
      little_user_id: body.little_user_id,
      family_id: body.family_id ?? null,
      pledge_class: body.pledge_class ?? null,
      confirmed_by_little: false,
      created_by: MOCK_CURRENT_USER.id,
      created_at: nowIso(),
    };
    MOCK_LINEAGE_TREE.edges.push(edge);
    const little = MOCK_LINEAGE_TREE.nodes.find((n) => n.user_id === body.little_user_id);
    const big = MOCK_LINEAGE_TREE.nodes.find((n) => n.user_id === body.big_user_id);
    if (little) {
      little.family_id = body.family_id ?? little.family_id;
      little.depth = (big?.depth ?? 0) + 1;
    }
    if (big && body.family_id) big.family_id = body.family_id;
    return mocked(edge);
  }
  return request<LineageEdgeOut>(`/chapters/${chapterId}/lineage/edges`, { method: "POST", body });
}

/** Little confirms their big — flips confirmed_by_little. */
export async function confirmEdge(chapterId: string, edgeId: string): Promise<LineageEdgeOut> {
  if (USE_MOCKS) {
    const edge = MOCK_LINEAGE_TREE.edges.find((e) => e.id === edgeId);
    if (edge) edge.confirmed_by_little = true;
    return mocked(edge ?? MOCK_LINEAGE_TREE.edges[0]);
  }
  return request<LineageEdgeOut>(`/chapters/${chapterId}/lineage/edges/${edgeId}/confirm`, {
    method: "POST",
  });
}
