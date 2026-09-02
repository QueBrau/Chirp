/** Feed API: campus FYP, chapter/org posts, likes, comments — routers/feed.py. */

import { request, requestWithHeaders } from "./client";

/** Who can see a post. 'org' (default) = chapter-public, visible to any member.
 * 'campus' = surfaces on the public GET /campuses/{campus_id}/feed. 'org_actives'
 * (board c102) = chapter-scoped like 'org', but only for a viewer whose OWN
 * membership is active — never offer this choice to a non-active member. */
export type PostAudience = "org" | "campus" | "org_actives";

export interface PostCreate {
  body: string;
  /** tmp/ object_name(s) from POST /media/upload-url (c132) — NOT a url. The server
   * moves the referenced tmp/ object to its permanent location and assigns the
   * resulting media_urls itself. */
  media_object_names?: string[] | null;
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
  media_object_names?: string[] | null; // see PostCreate.media_object_names
  post_type?: PostType;
  duration_sec?: number | null;
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
 * server-side, so screens never have to fan out to a per-post likes or
 * comments request.
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

/**
 * A comment plus its author's display identity (c228).
 *
 * The identity fields are pre-joined server-side, exactly like FeedPostOut's, and for
 * a harder reason: nothing in this API turns a user id into a person. There is no GET
 * /users/{id}, and the chapter roster only covers chapters the caller belongs to — so
 * without display_name a thread renders as a column of raw UUIDs, and with one
 * request per comment it would not render at all on a slow connection.
 *
 * display_name is non-null: routers/feed.py INNER JOINs users on author_id.
 */
export interface PostCommentOut {
  id: string;
  post_id: string;
  author_id: string;
  body: string;
  created_at: string;
  deleted_at: string | null;
  display_name: string;
  avatar_url: string | null;
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

/** Result of listPosts (board c102) — the posts, plus the honest-signal flag for a
 * non-active viewer: whether this chapter has actives-only content they cannot see.
 * Always false for an active member, who already sees everything there is. */
export interface ChapterFeedResult {
  posts: FeedPostOut[];
  activesOnlyHidden: boolean;
}

/**
 * Reverse-chron chapter/org feed (any audience the caller can see) — the org's own
 * posts, FeedPostOut shape.
 *
 * The response BODY is still the bare FeedPostOut[] it always was (matches the
 * backend's own list[FeedPostOut] return type and every existing pytest
 * assertion) — `activesOnlyHidden` rides the X-Actives-Only-Hidden response
 * header instead, via requestWithHeaders, precisely so this stays a drop-in
 * shape and the only thing that changes is one new field on the result.
 */
export async function listPosts(chapterId: string): Promise<ChapterFeedResult> {
  const { data, headers } = await requestWithHeaders<FeedPostOut[]>(
    `/chapters/${chapterId}/posts`,
  );
  return { posts: data, activesOnlyHidden: headers.get("x-actives-only-hidden") === "true" };
}

export async function createPost(chapterId: string, body: PostCreate): Promise<PostOut> {
  return request<PostOut>(`/chapters/${chapterId}/posts`, { method: "POST", body });
}

/** Response of GET /chapters/{chapter_id}/posts/count (board c217). No user id in
 * it, because there is none in the request: the server counts the CALLER'S posts. */
export interface MyPostCountOut {
  chapter_id: string;
  count: number;
}

/**
 * How many posts the caller has written in this chapter, as one server-side
 * aggregate (board c217).
 *
 * Do NOT go back to listPosts().then(filter by author_id) for this. That is what
 * the profile screen used to do, and c210 capped the list route at 50 with a
 * cursor, so past 50 posts the filter silently returned a number that was too
 * small. The server applies the same visibility rules the listing does, so this
 * count and the feed always agree.
 */
export async function countMyPosts(chapterId: string): Promise<MyPostCountOut> {
  return request<MyPostCountOut>(`/chapters/${chapterId}/posts/count`);
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

/**
 * Like a post. PUT, not POST: the backend registers this path as PUT only
 * (routers/feed.py `like_post`), because the handler is an idempotent upsert —
 * a double-tap re-sends the same like rather than creating a second one. A POST
 * here never reached the handler at all; FastAPI answered 405 at routing time,
 * so every like tap failed while unliking (DELETE) kept working.
 */
export async function likePost(postId: string): Promise<PostLikeOut> {
  return request<PostLikeOut>(`/posts/${postId}/likes`, { method: "PUT" });
}

export async function unlikePost(postId: string): Promise<void> {
  return request<void>(`/posts/${postId}/likes`, { method: "DELETE" });
}

/** One page of a comment thread. `before`/`beforeId` are the OLDEST comment you
 * already hold; the result is the page immediately before it, still oldest-first,
 * ready to prepend. Omit them for the newest page (c258). */
export interface CommentPageQuery {
  before?: string;
  beforeId?: string;
  limit?: number;
}

export async function listComments(
  postId: string,
  options: CommentPageQuery = {},
): Promise<PostCommentOut[]> {
  // Params go through request()'s `query` option, same as listMessages, rather than
  // being interpolated into the path. Two reasons, and the second one bit: the path
  // stays a literal that scripts/verify-contract.mjs can match against the backend
  // route table, and a hand-built `?${suffix}` made that checker report the route as
  // missing entirely.
  //
  // BOTH cursor halves or NEITHER. The server accepts `before` alone for legacy
  // callers, but that form cannot tie-break comments sharing a timestamp and drops
  // them at a page boundary, so this client never sends the half-cursor.
  const paired = options.before !== undefined && options.beforeId !== undefined;
  return request<PostCommentOut[]>(`/posts/${postId}/comments`, {
    query: {
      before: paired ? options.before : undefined,
      before_id: paired ? options.beforeId : undefined,
      limit: options.limit,
    },
  });
}

export async function createComment(
  postId: string,
  body: PostCommentCreate,
): Promise<PostCommentOut> {
  return request<PostCommentOut>(`/posts/${postId}/comments`, { method: "POST", body });
}
