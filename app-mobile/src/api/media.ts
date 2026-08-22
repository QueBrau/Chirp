/** Signed upload URLs for post media (c70) — routers/media.py. */

import { request } from "./client";

/** JPEG/PNG/WebP only, alpha scope. Keep in sync with backend's ALLOWED_CONTENT_TYPES. */
export type AllowedMediaContentType = "image/jpeg" | "image/png" | "image/webp";

/** Alpha decision, Aug 22 — mirrors backend's MAX_UPLOAD_BYTES. */
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

export interface MediaUploadUrlOut {
  upload_url: string;
  public_url: string;
  expires_in_seconds: number;
}

/**
 * Request a signed PUT url, then PUT the bytes to it directly — this call never
 * touches image bytes itself, matching the backend: client uploads straight to GCS,
 * never proxied through the API. The caller stores `public_url` (not `upload_url`)
 * into a post's media_urls once the PUT below succeeds.
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
 */
export async function uploadMediaBytes(
  uploadUrl: string,
  bytes: Blob,
  contentType: AllowedMediaContentType,
): Promise<void> {
  const response = await fetch(uploadUrl, {
    method: "PUT",
    headers: { "Content-Type": contentType },
    body: bytes,
  });
  if (!response.ok) {
    throw new Error(`Upload failed: ${response.status}`);
  }
}
