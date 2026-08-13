/** Feed API: chapter posts, likes, comments — routers/feed.py. */

import { mocked, request, USE_MOCKS } from "./client";
import {
  MOCK_CURRENT_USER,
  MOCK_POST_COMMENTS,
  MOCK_POST_LIKES,
  MOCK_POSTS,
  newMockId,
  nowIso,
} from "../mocks/data";

export interface PostCreate {
  body: string;
  media_urls?: string[] | null;
}

export interface PostUpdate {
  body?: string | null;
  media_urls?: string[] | null;
}

/** DESIGN §7 FYP: drives which MediaPostCard layout a post renders as. */
export type PostType = "text" | "photo" | "video";

/** DESIGN §7 filter pills: which feed tab a post surfaces under. */
export type PostSource = "forYou" | "campus" | "org";

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
  /** Optional; absent = "org" (chapter-scoped, the pre-FYP default). */
  source?: PostSource;
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

/** Reverse-chron chapter feed (v1). */
export async function listPosts(chapterId: string): Promise<PostOut[]> {
  if (USE_MOCKS) {
    return mocked(
      MOCK_POSTS.filter((p) => p.chapter_id === chapterId && p.deleted_at === null).sort((a, b) =>
        b.created_at.localeCompare(a.created_at),
      ),
    );
  }
  return request<PostOut[]>(`/chapters/${chapterId}/posts`);
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
