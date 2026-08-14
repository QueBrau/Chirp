/** Feed API: chapter posts, likes, comments — routers/feed.py. */

import { request } from "./client";

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
  return request<PostOut[]>(`/chapters/${chapterId}/posts`);
}

export async function createPost(chapterId: string, body: PostCreate): Promise<PostOut> {
  return request<PostOut>(`/chapters/${chapterId}/posts`, { method: "POST", body });
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
