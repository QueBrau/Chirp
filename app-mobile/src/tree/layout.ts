/** Tidy tree layout — branching big→littles (one big per little), forest of family trees. */

import type { FamilyOut, LineageEdgeOut, LineageNodeOut, LineageTreeOut } from "@/api/lineage";
import { initials } from "@/lib/initials";

export interface GraphNode {
  id: string;
  displayName: string;
  shortName: string;
  isGhost: boolean;
  familyId: string | null;
  familyColor: string;
  pledgeClass: string | null;
  initials: string;
  depth: number;
  x: number;
  y: number;
}

export interface GraphLink {
  id: string;
  sourceId: string;
  targetId: string;
  confirmed: boolean;
  familyId: string | null;
  familyColor: string;
}

export interface GraphLayout {
  nodes: GraphNode[];
  links: GraphLink[];
  families: FamilyOut[];
  width: number;
  height: number;
}

const UNPLACED_COLOR = "#8A8A94";
const ROW_GAP = 88;
const LEAF_GAP = 120;
const TREE_GAP = 140;
const PAD = 96;

interface LayoutNode {
  id: string;
  depth: number;
  children: LayoutNode[];
  /** Subtree width in leaf-gap units. */
  width: number;
  x: number;
  y: number;
}

function shortName(name: string): string {
  const cleaned = name.replace(/["']/g, "");
  const first = cleaned.split(/\s+/).filter(Boolean)[0] ?? name;
  return first.length > 12 ? `${first.slice(0, 11)}…` : first;
}

function familyColorMap(families: FamilyOut[]): Map<string, string> {
  return new Map(families.map((f) => [f.id, f.color]));
}

function buildChildrenMap(edges: LineageEdgeOut[]): Map<string, string[]> {
  const childrenOf = new Map<string, string[]>();
  for (const edge of edges) {
    const list = childrenOf.get(edge.big_user_id) ?? [];
    list.push(edge.little_user_id);
    childrenOf.set(edge.big_user_id, list);
  }
  return childrenOf;
}

/** Measure + place a tidy tree: parent centered over its children. */
function layoutTree(rootId: string, childrenOf: Map<string, string[]>, depth = 0): LayoutNode {
  const childIds = childrenOf.get(rootId) ?? [];
  const children = childIds.map((id) => layoutTree(id, childrenOf, depth + 1));

  let width = LEAF_GAP;
  if (children.length === 1) {
    width = children[0].width;
  } else if (children.length > 1) {
    width = children.reduce((sum, child) => sum + child.width, 0);
  }

  const node: LayoutNode = { id: rootId, depth, children, width, x: 0, y: depth * ROW_GAP };

  if (children.length === 0) {
    node.x = 0;
    return node;
  }

  // Place children left-to-right, then center parent on their span.
  let cursor = 0;
  for (const child of children) {
    shiftTree(child, cursor + child.width / 2);
    cursor += child.width;
  }
  const left = children[0].x - children[0].width / 2;
  const right = children[children.length - 1].x + children[children.length - 1].width / 2;
  node.x = (left + right) / 2;
  return node;
}

function shiftTree(node: LayoutNode, targetX: number): void {
  const dx = targetX - node.x;
  const walk = (n: LayoutNode) => {
    n.x += dx;
    n.children.forEach(walk);
  };
  walk(node);
}

function collectPositions(node: LayoutNode, into: Map<string, { x: number; y: number; depth: number }>): void {
  into.set(node.id, { x: node.x, y: node.y, depth: node.depth });
  node.children.forEach((child) => collectPositions(child, into));
}

/**
 * Lay out linked members as a forest of tidy trees (one root per family head).
 * One big per little → each node has ≤1 parent; branching comes from multiple littles.
 */
export function layoutLineageGraph(tree: LineageTreeOut): GraphLayout {
  const colors = familyColorMap(tree.families);
  const linkedIds = new Set<string>();
  for (const edge of tree.edges) {
    linkedIds.add(edge.big_user_id);
    linkedIds.add(edge.little_user_id);
  }

  const placedNodes = tree.nodes.filter((n) => linkedIds.has(n.user_id));
  const childrenOf = buildChildrenMap(tree.edges);
  const littles = new Set(tree.edges.map((e) => e.little_user_id));
  const roots = placedNodes.filter((n) => !littles.has(n.user_id));

  // Stable family order, then any root without a family.
  const familyOrder = tree.families.map((f) => f.id);
  const rootsSorted = [...roots].sort((a, b) => {
    const ai = a.family_id ? familyOrder.indexOf(a.family_id) : 999;
    const bi = b.family_id ? familyOrder.indexOf(b.family_id) : 999;
    if (ai !== bi) return ai - bi;
    return a.display_name.localeCompare(b.display_name);
  });

  const positions = new Map<string, { x: number; y: number; depth: number }>();
  let cursor = 0;

  for (const root of rootsSorted) {
    const laid = layoutTree(root.user_id, childrenOf, 0);
    const half = laid.width / 2;
    shiftTree(laid, cursor + half);
    collectPositions(laid, positions);
    cursor += laid.width + TREE_GAP;
  }

  // Orphans that somehow missed placement.
  for (const node of placedNodes) {
    if (!positions.has(node.user_id)) {
      positions.set(node.user_id, { x: cursor, y: 0, depth: 0 });
      cursor += LEAF_GAP;
    }
  }

  const nodes: GraphNode[] = placedNodes.map((node) => {
    const pos = positions.get(node.user_id) ?? { x: 0, y: 0, depth: 0 };
    return {
      id: node.user_id,
      displayName: node.display_name,
      shortName: shortName(node.display_name),
      isGhost: node.is_ghost,
      familyId: node.family_id,
      familyColor: node.family_id ? (colors.get(node.family_id) ?? UNPLACED_COLOR) : UNPLACED_COLOR,
      pledgeClass: node.pledge_class,
      initials: initials(node.display_name),
      depth: pos.depth,
      x: pos.x,
      y: pos.y,
    };
  });

  const nodeById = new Map(nodes.map((n) => [n.id, n]));
  const links: GraphLink[] = tree.edges
    .filter((edge) => nodeById.has(edge.big_user_id) && nodeById.has(edge.little_user_id))
    .map((edge) => ({
      id: edge.id,
      sourceId: edge.big_user_id,
      targetId: edge.little_user_id,
      confirmed: edge.confirmed_by_little,
      familyId: edge.family_id,
      familyColor: edge.family_id ? (colors.get(edge.family_id) ?? UNPLACED_COLOR) : UNPLACED_COLOR,
    }));

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const node of nodes) {
    minX = Math.min(minX, node.x);
    minY = Math.min(minY, node.y);
    maxX = Math.max(maxX, node.x);
    maxY = Math.max(maxY, node.y);
  }
  if (!Number.isFinite(minX)) {
    minX = 0;
    minY = 0;
    maxX = 0;
    maxY = 0;
  }

  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  for (const node of nodes) {
    node.x -= cx;
    node.y -= cy;
  }

  return {
    nodes,
    links,
    families: tree.families,
    width: Math.max(320, maxX - minX + PAD * 2),
    height: Math.max(320, maxY - minY + PAD * 2),
  };
}

export function neighborIds(tree: LineageTreeOut, userId: string): Set<string> {
  const ids = new Set<string>([userId]);
  for (const edge of tree.edges) {
    if (edge.big_user_id === userId) ids.add(edge.little_user_id);
    if (edge.little_user_id === userId) ids.add(edge.big_user_id);
  }
  return ids;
}

export function edgesForUser(tree: LineageTreeOut, userId: string): LineageEdgeOut[] {
  return tree.edges.filter((e) => e.big_user_id === userId || e.little_user_id === userId);
}

export function unplacedCount(tree: LineageTreeOut): number {
  const linked = new Set<string>();
  for (const edge of tree.edges) {
    linked.add(edge.big_user_id);
    linked.add(edge.little_user_id);
  }
  return tree.nodes.filter((n) => !linked.has(n.user_id)).length;
}
