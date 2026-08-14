/** Feed API: campus FYP, chapter/org posts, likes, comments — routers/feed.py. */

import { mocked, request, USE_MOCKS } from "./client";
import {
  MOCK_CURRENT_USER,
  MOCK_POST_COMMENTS,
  MOCK_POST_LIKES,
  MOCK_POSTS,
  mockCampusIdForChapter,
  mockUserById,
  newMockId,
  nowIso,
} from "../mocks/data";

/** Who can see a post. 'org' (default) = chapter-private, never on the FYP.
 * 'campus' = surfaces on the public GET /campuses/{campus_id}/feed. */
export type PostAudience = "org" | "campus";

export interface PostCreate {
  body: string;
  media_urls?: string[] | null;
  /** Server defaults to "org" when omitted (routers/feed.py). */
  audience?: PostAudience;
}

export interface PostUpdate {
  body?: string | null;
  media_urls?: string[] | null;
}

/** DESIGN §7 FYP: drives which MediaPostCard layout a post renders as. */
export type PostType = "text" | "photo" | "video";

export interface PostOut {
  id: string;
  chapter_id: string;
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

/** Attach author display fields + engagement counts to a raw mock post, matching
 * the shape the real FeedPostOut-returning endpoints send over the wire. */
function toFeedPostOut(post: PostOut): FeedPostOut {
  const author = mockUserById(post.author_id);
  const likeCount = MOCK_POST_LIKES.filter((l) => l.post_id === post.id).length;
  const likedByMe = MOCK_POST_LIKES.some(
    (l) => l.post_id === post.id && l.user_id === MOCK_CURRENT_USER.id,
  );
  const commentCount = MOCK_POST_COMMENTS.filter(
    (c) => c.post_id === post.id && c.deleted_at === null,
  ).length;
  return {
    ...post,
    display_name: author?.display_name ?? "Unknown",
    avatar_url: author?.avatar_url ?? null,
    like_count: likeCount,
    comment_count: commentCount,
    liked_by_me: likedByMe,
  };
}

/**
 * Public campus FYP (audience="campus" only — org-private posts never appear
 * here, enforced server-side): GET /campuses/{campus_id}/feed.
 */
export async function listCampusFeed(
  campusId: string,
  opts: ListFeedOptions = {},
): Promise<FeedPostOut[]> {
  if (USE_MOCKS) {
    let posts = MOCK_POSTS.filter(
      (p) =>
        p.audience === "campus" &&
        p.deleted_at === null &&
        mockCampusIdForChapter(p.chapter_id) === campusId,
    ).sort((a, b) => b.created_at.localeCompare(a.created_at));
    if (opts.before !== undefined) {
      posts = posts.filter((p) => p.created_at < opts.before!);
    }
    if (opts.limit !== undefined) posts = posts.slice(0, opts.limit);
    return mocked(posts.map(toFeedPostOut));
  }
  return request<FeedPostOut[]>(`/campuses/${campusId}/feed`, {
    query: { limit: opts.limit, before: opts.before, before_id: opts.before_id },
  });
}

/** Reverse-chron chapter/org feed (any audience) — the org's own posts, FeedPostOut shape. */
export async function listPosts(chapterId: string): Promise<FeedPostOut[]> {
  if (USE_MOCKS) {
    const posts = MOCK_POSTS.filter((p) => p.chapter_id === chapterId && p.deleted_at === null).sort(
      (a, b) => b.created_at.localeCompare(a.created_at),
    );
    return mocked(posts.map(toFeedPostOut));
  }
  return request<FeedPostOut[]>(`/chapters/${chapterId}/posts`);
}

export async function createPost(chapterId: string, body: PostCreate): Promise<PostOut> {
  if (USE_MOCKS) {
    const post: PostOut = {
      id: newMockId("post"),
      chapter_id: chapterId,
      author_id: MOCK_CURRENT_USER.id,
      body: body.body,
      media_urls: body.media_urls ?? null,
      created_at: nowIso(),
      deleted_at: null,
      audience: body.audience ?? "org",
    };
    MOCK_POSTS.push(post);
    return mocked(post);
  }
  return request<PostOut>(`/chapters/${chapterId}/posts`, { method: "POST", body });
}

export async function deletePost(chapterId: string, postId: string): Promise<void> {
  if (USE_MOCKS) {
    const post = MOCK_POSTS.find((p) => p.id === postId);
    if (post) post.deleted_at = nowIso();
    return mocked(undefined);
  }
  return request<void>(`/chapters/${chapterId}/posts/${postId}`, { method: "DELETE" });
}

export async function listLikes(postId: string): Promise<PostLikeOut[]> {
  if (USE_MOCKS) return mocked(MOCK_POST_LIKES.filter((l) => l.post_id === postId));
  return request<PostLikeOut[]>(`/posts/${postId}/likes`);
}

export async function likePost(postId: string): Promise<PostLikeOut> {
  if (USE_MOCKS) {
    const existing = MOCK_POST_LIKES.find(
      (l) => l.post_id === postId && l.user_id === MOCK_CURRENT_USER.id,
    );
    if (existing) return mocked(existing);
    const like: PostLikeOut = { post_id: postId, user_id: MOCK_CURRENT_USER.id, created_at: nowIso() };
    MOCK_POST_LIKES.push(like);
    return mocked(like);
  }
  return request<PostLikeOut>(`/posts/${postId}/likes`, { method: "POST" });
}

export async function unlikePost(postId: string): Promise<void> {
  if (USE_MOCKS) {
    const index = MOCK_POST_LIKES.findIndex(
      (l) => l.post_id === postId && l.user_id === MOCK_CURRENT_USER.id,
    );
    if (index >= 0) MOCK_POST_LIKES.splice(index, 1);
    return mocked(undefined);
  }
  return request<void>(`/posts/${postId}/likes`, { method: "DELETE" });
}

export async function listComments(postId: string): Promise<PostCommentOut[]> {
  if (USE_MOCKS) {
    return mocked(
      MOCK_POST_COMMENTS.filter((c) => c.post_id === postId && c.deleted_at === null),
    );
  }
  return request<PostCommentOut[]>(`/posts/${postId}/comments`);
}

export async function createComment(
  postId: string,
  body: PostCommentCreate,
): Promise<PostCommentOut> {
  if (USE_MOCKS) {
    const comment: PostCommentOut = {
      id: newMockId("cmt"),
      post_id: postId,
      author_id: MOCK_CURRENT_USER.id,
      body: body.body,
      created_at: nowIso(),
      deleted_at: null,
    };
    MOCK_POST_COMMENTS.push(comment);
    return mocked(comment);
  }
  return request<PostCommentOut>(`/posts/${postId}/comments`, { method: "POST", body });
}
