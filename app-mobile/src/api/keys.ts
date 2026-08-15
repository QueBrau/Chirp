/** E2EE key-directory API: device registration, prekey upload/count, prekey bundles — routers/keys.py. */

import { request } from "./client";

export interface SignedPrekeyCreate {
  key_id: number;
  public_key_b64: string;
  signature_b64: string;
}

export interface OneTimePrekeyCreate {
  key_id: number;
  public_key_b64: string;
}

/** Mirrors backend DeviceCreate — identity key + signed prekey + one-time prekey batch. */
export interface DeviceCreate {
  device_label?: string | null;
  registration_id: number;
  identity_key_b64: string;
  signed_prekey: SignedPrekeyCreate;
  one_time_prekeys: OneTimePrekeyCreate[];
}

export interface DeviceOut {
  id: string;
  user_id: string;
  device_label: string | null;
  registration_id: number;
  identity_key_b64: string;
  created_at: string;
  revoked_at: string | null;
}

/** Replenish one-time prekeys and/or rotate the signed prekey. */
export interface PrekeyUpload {
  signed_prekey?: SignedPrekeyCreate | null;
  one_time_prekeys: OneTimePrekeyCreate[];
}

export interface PrekeyCountOut {
  device_id: string;
  one_time_prekeys_available: number;
}

export interface SignedPrekeyOut {
  key_id: number;
  public_key_b64: string;
  signature_b64: string;
}

export interface OneTimePrekeyOut {
  key_id: number;
  public_key_b64: string;
}

/** One recipient device's bundle; one_time_prekey is omitted when the pool is empty. */
export interface DevicePrekeyBundleOut {
  device_id: string;
  registration_id: number;
  identity_key_b64: string;
  signed_prekey: SignedPrekeyOut;
  one_time_prekey: OneTimePrekeyOut | null;
}

/** Full bundle for a user: one entry per non-revoked device. */
export interface PrekeyBundleOut {
  user_id: string;
  devices: DevicePrekeyBundleOut[];
}

export async function registerDevice(body: DeviceCreate): Promise<DeviceOut> {
  return request<DeviceOut>("/devices", { method: "POST", body });
}

export async function uploadPrekeys(deviceId: string, body: PrekeyUpload): Promise<PrekeyCountOut> {
  return request<PrekeyCountOut>(`/devices/${deviceId}/prekeys`, { method: "POST", body });
}

export async function getPrekeyCount(deviceId: string): Promise<PrekeyCountOut> {
  return request<PrekeyCountOut>(`/devices/${deviceId}/prekeys/count`);
}

/** Fetch a user's prekey bundle to start sessions (server consumes one OTK per device atomically). */
export async function getPrekeyBundle(userId: string): Promise<PrekeyBundleOut> {
  return request<PrekeyBundleOut>(`/users/${userId}/prekey-bundle`);
}
