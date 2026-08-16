/** Feed API: campus FYP, chapter/org posts, likes, comments — routers/feed.py. */

import { request } from "./client";

/** Who can see a post. 'org' (default) = chapter-private, never on the FYP.
 * 'campus' = surfaces on the public GET /campuses/{campus_id}/feed. */
export type PostAudience = "org" | "campus";

export interface PostCreate {
  body: string;
  media_urls?: string[] | null;
  /** Server defaults to "org" when omitted (routers/feed.py). */
  audience?: PostAudience;
  /** Server defaults to "text" — text compose omits this; declared now so a future media composer does not widen this type again. */
  post_type?: PostType;
  duration_sec?: number | null;
}

/**
 * Body for POST /campuses/{campus_id}/posts — the route a student with no chapter
 * uses (c71). No `audience` field on purpose: the server hard-codes 'campus' there,
 * because 'org' means chapter-private and this caller may have no chapter at all.
 */
export interface CampusPostCreate {
  body: string;
  media_urls?: string[] | null;
  post_type?: PostType;
  duration_sec?: number | null;
}

export interface PostUpdate {
  body?: string | null;
  media_urls?: string[] | null;
}

/** DESIGN §7 FYP: drives which MediaPostCard layout a post renders as. */
export type PostType = "text" | "photo" | "video";

export interface PostOut {
  id: string;
  /** null when a chapter-less student authored it (c71); always set on an org post. */
  chapter_id: string | null;
  campus_id: string;
  author_id: string;
  body: string;
  media_urls: string[] | null;
  created_at: string;
  deleted_at: string | null;
  /** Optional so pre-FYP call sites (e.g. createPost) still typecheck. Absent = "text". */
  post_type?: PostType;
  /** Video posts only. */
  duration_sec?: number | null;
  /** Who can see this post — always present on rows the backend returns. */
  audience: PostAudience;
}

/**
 * FeedPostOut: the shape both feed list endpoints actually return — a post
 * row plus the author display fields and engagement counts pre-joined
 * server-side, so screens never have to fan out to listLikes/listComments
 * per post.
 */
export interface FeedPostOut extends PostOut {
  display_name: string;
  avatar_url: string | null;
  like_count: number;
  comment_count: number;
  liked_by_me: boolean;
}

export interface PostLikeOut {
  post_id: string;
  user_id: string;
  created_at: string;
}

export interface PostCommentCreate {
  body: string;
}

export interface PostCommentOut {
  id: string;
  post_id: string;
  author_id: string;
  body: string;
  created_at: string;
  deleted_at: string | null;
}

export interface ListFeedOptions {
  limit?: number;
  /** created_at cursor — posts older than this. */
  before?: string;
  before_id?: string;
}

/**
 * Public campus FYP (audience="campus" only — org-private posts never appear
 * here, enforced server-side): GET /campuses/{campus_id}/feed.
 */
export async function listCampusFeed(
  campusId: string,
  opts: ListFeedOptions = {},
): Promise<FeedPostOut[]> {
  return request<FeedPostOut[]>(`/campuses/${campusId}/feed`, {
    query: { limit: opts.limit, before: opts.before, before_id: opts.before_id },
  });
}

/** Reverse-chron chapter/org feed (any audience) — the org's own posts, FeedPostOut shape. */
export async function listPosts(chapterId: string): Promise<FeedPostOut[]> {
  return request<FeedPostOut[]>(`/chapters/${chapterId}/posts`);
}

export async function createPost(chapterId: string, body: PostCreate): Promise<PostOut> {
  return request<PostOut>(`/chapters/${chapterId}/posts`, { method: "POST", body });
}

/**
 * Post straight to the campus feed, with no chapter involved (c71). This is the
 * only create route available to a student who belongs to no org, and it always
 * produces a campus-audience post.
 */
export async function createCampusPost(
  campusId: string,
  body: CampusPostCreate,
): Promise<PostOut> {
  return request<PostOut>(`/campuses/${campusId}/posts`, { method: "POST", body });
}

export async function deletePost(chapterId: string, postId: string): Promise<void> {
  return request<void>(`/chapters/${chapterId}/posts/${postId}`, { method: "DELETE" });
}

export async function listLikes(postId: string): Promise<PostLikeOut[]> {
  return request<PostLikeOut[]>(`/posts/${postId}/likes`);
}

export async function likePost(postId: string): Promise<PostLikeOut> {
  return request<PostLikeOut>(`/posts/${postId}/likes`, { method: "POST" });
}

export async function unlikePost(postId: string): Promise<void> {
  return request<void>(`/posts/${postId}/likes`, { method: "DELETE" });
}

export async function listComments(postId: string): Promise<PostCommentOut[]> {
  return request<PostCommentOut[]>(`/posts/${postId}/comments`);
}

export async function createComment(
  postId: string,
  body: PostCommentCreate,
): Promise<PostCommentOut> {
  return request<PostCommentOut>(`/posts/${postId}/comments`, { method: "POST", body });
}
