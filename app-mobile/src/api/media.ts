/** Signed upload URLs for post media (c70) — routers/media.py. */

import { request } from "./client";

/** JPEG/PNG/WebP only, alpha scope. Keep in sync with backend's ALLOWED_CONTENT_TYPES. */
export type AllowedMediaContentType = "image/jpeg" | "image/png" | "image/webp";

/** Alpha decision, Aug 22 — mirrors backend's MAX_UPLOAD_BYTES. */
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

export interface MediaUploadUrlOut {
  upload_url: string;
  /** tmp/ location (c132) — fetchable for local preview only, never sent to a post.
   * A post's real media_urls is server-assigned by finalize_media_object() at
   * create/update time, from `object_name` below, not from this url. */
  preview_url: string;
  object_name: string;
  expires_in_seconds: number;
}

/**
 * Request a signed PUT url, then PUT the bytes to it directly — this call never
 * touches image bytes itself, matching the backend: client uploads straight to GCS,
 * never proxied through the API. The caller keeps `preview_url` only for local
 * rendering and sends `object_name` (not a url) in a post's media_object_names once
 * the PUT below succeeds (c132) — the server moves that tmp/ object and assigns the
 * post's permanent media_urls itself.
 */
export async function getMediaUploadUrl(
  contentType: AllowedMediaContentType,
  byteSize: number,
): Promise<MediaUploadUrlOut> {
  return request<MediaUploadUrlOut>("/media/upload-url", {
    method: "POST",
    body: { content_type: contentType, byte_size: byteSize },
  });
}

/**
 * PUT raw bytes to a signed url from getMediaUploadUrl(). Plain fetch, not the
 * shared `request()` client: this call goes straight to GCS, never to the Chirp API,
 * so it must carry NEITHER the Firebase bearer token nor a JSON content-type — the
 * signed url's own signature is scoped to exactly the content-type and size range
 * the backend issued it for, and adding an Authorization header GCS never asked to
 * sign would only risk a mismatched-signature rejection.
 *
 * c133: storage_service.py signs X-Goog-Content-Length-Range as part of the url's
 * signed headers, which means GCS then REQUIRES that exact header on the PUT itself
 * — omitting it 400s with MalformedSecurityHeader (found by manager's real-bucket
 * verification, reproduced and fixed here). Value is derived from MAX_UPLOAD_BYTES,
 * not a second hardcoded byte count, so it cannot drift from the cap.
 */
export async function uploadMediaBytes(
  uploadUrl: string,
  bytes: Blob,
  contentType: AllowedMediaContentType,
): Promise<void> {
  const response = await fetch(uploadUrl, {
    method: "PUT",
    headers: {
      "Content-Type": contentType,
      "X-Goog-Content-Length-Range": `1,${MAX_UPLOAD_BYTES}`,
    },
    body: bytes,
  });
  if (!response.ok) {
    throw new Error(`Upload failed: ${response.status}`);
  }
}
