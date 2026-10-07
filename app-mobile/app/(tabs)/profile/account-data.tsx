/** Account export and deletion request status. */

import { Feather } from "@expo/vector-icons";
import { useCallback, useEffect, useRef, useState } from "react";
import { Pressable, View } from "react-native";

import {
  createDataRequest,
  listDataRequests,
  type DataRequestKind,
  type DataRequestOut,
  downloadDataRequest,
} from "@/api/dataRequests";
import { ApiError } from "@/api/client";
import { AppText, Button, Card, EmptyState, Screen } from "@/components";
import { confirmAction, showAlert, showApiError } from "@/lib/alert";
import { shareJson } from "@/lib/export";
import { radii, spacing, useTheme } from "@/theme";
import { currentIdentity, onIdentityChanged, ownsIdentity, type AuthIdentity } from "@/auth/identity";
import { openLegalLink } from "@/lib/legalLinks";

function showRequestError(error: unknown, title: string) {
  if (error instanceof ApiError && error.detail === "recent_authentication_required") {
    showAlert("Sign in again", "For your privacy, sign out and sign back in before requesting or downloading account data.");
  } else {
    showApiError(error, title);
  }
}

function statusLabel(status: DataRequestOut["status"]): string {
  return status.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function RequestCard({ item, owner, onRefresh }: { item: DataRequestOut; owner: AuthIdentity; onRefresh: () => void }) {
  const palette = useTheme();
  const downloading = useRef(false);
  const [downloadBusy, setDownloadBusy] = useState(false);
  const isExport = item.kind === "export";
  const canDownload = isExport && (item.status === "ready" || item.status === "partially_completed");

  const download = async () => {
    if (downloading.current || !ownsIdentity(owner)) return;
    downloading.current = true;
    setDownloadBusy(true);
    try {
      const payload = await downloadDataRequest(item.id);
      if (!ownsIdentity(owner)) return;
      await shareJson(`chirp-account-export-${item.id}.json`, payload, owner);
    } catch (error) {
      if (ownsIdentity(owner)) showRequestError(error, "Couldn't share export");
    } finally {
      downloading.current = false;
      if (ownsIdentity(owner)) setDownloadBusy(false);
    }
  };

  return (
    <Card>
      <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
        <View
          style={{
            width: spacing.xxl,
            height: spacing.xxl,
            borderRadius: radii.pill,
            backgroundColor: isExport ? palette.accentSoft : palette.dangerSoft,
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <Feather name={isExport ? "download" : "trash-2"} size={18} color={isExport ? palette.accent : palette.danger} />
        </View>
        <View style={{ flex: 1, gap: spacing.xs }}>
          <AppText variant="headline">{isExport ? "Data copy" : "Account deletion"}</AppText>
          <AppText variant="caption" tone="secondary">{statusLabel(item.status)}</AppText>
        </View>
        <AppText variant="caption" tone={item.status === "failed" || item.status === "blocked" ? "danger" : "secondary"}>
          {new Date(item.updated_at).toLocaleDateString()}
        </AppText>
      </View>
      {item.scope.length > 0 ? (
        <AppText variant="caption" tone="secondary" style={{ marginTop: spacing.md }}>
          Included: {item.scope.join(", ")}.
        </AppText>
      ) : null}
      {item.excluded.length > 0 ? (
        <AppText variant="caption" tone="secondary" style={{ marginTop: spacing.sm }}>
          Some copies need separate handling: {item.excluded.join(", ")}.
        </AppText>
      ) : null}
      {item.retention_reasons.length > 0 ? (
        <AppText variant="caption" tone="secondary" style={{ marginTop: spacing.sm }}>
          Some records may remain where required for financial, safety, shared organization, or legal obligations.
        </AppText>
      ) : null}
      {item.failure_code ? (
        <AppText variant="caption" tone="danger" style={{ marginTop: spacing.sm }}>
          Your request is recorded. Our team still needs to review and complete the deletion. Your account has not been deleted. Reference: {item.id}.
        </AppText>
      ) : null}
      <View style={{ flexDirection: "row", gap: spacing.sm, marginTop: spacing.md }}>
        {canDownload ? <Button label={downloadBusy ? "Preparing copy..." : "Download copy"} disabled={downloadBusy} onPress={() => void download()} /> : null}
        <Button label="Refresh status" variant="neutral" onPress={onRefresh} />
      </View>
    </Card>
  );
}

export default function AccountDataScreen() {
  const [owner, setOwner] = useState(currentIdentity);
  const ownerRef = useRef(owner);
  const mountedRef = useRef(true);
  const generationRef = useRef(0);
  const submitGenerationRef = useRef(0);
  const submitBusyRef = useRef(false);
  const [items, setItems] = useState<DataRequestOut[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState<DataRequestKind | null>(null);

  const load = useCallback(async () => {
    const requestedOwner = ownerRef.current;
    const generation = ++generationRef.current;
    const current = () => mountedRef.current && ownsIdentity(requestedOwner) && generationRef.current === generation;
    setLoading(true);
    try {
      const result = await listDataRequests();
      if (current()) setItems(result);
    } catch (error) {
      if (current()) {
        setItems(null);
        showApiError(error, "Couldn't load your data requests");
      }
    } finally {
      if (current()) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    const reset = () => {
      const next = currentIdentity();
      ownerRef.current = next;
      generationRef.current += 1;
      submitGenerationRef.current += 1;
      submitBusyRef.current = false;
      setSubmitting(null);
      setItems(null);
      setOwner(next);
    };
    const unsubscribe = onIdentityChanged(reset);
    if (!ownsIdentity(ownerRef.current)) reset();
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
      submitGenerationRef.current += 1;
      unsubscribe();
    };
  }, []);

  useEffect(() => {
    setItems(null);
    void load();
  }, [load, owner]);

  const submit = (kind: DataRequestKind) => {
    const requestedOwner = ownerRef.current;
    if (submitBusyRef.current || !ownsIdentity(requestedOwner)) return;
    const isDeletion = kind === "deletion";
    confirmAction({
      title: isDeletion ? "Request account deletion?" : "Request a copy of your data?",
      message: isDeletion
        ? "This starts a review of your account and associated records. It does not erase shared, financial, safety, or legally required records automatically. You can contact support about the scope before fulfillment."
        : "We will prepare the account data currently available for export. Records held by payment, authentication, email, storage, or other providers may need separate handling.",
      confirmLabel: isDeletion ? "Request deletion" : "Request copy",
      destructive: isDeletion,
      onConfirm: () => {
        if (!mountedRef.current || !ownsIdentity(requestedOwner) || submitBusyRef.current) return;
        submitBusyRef.current = true;
        const generation = ++submitGenerationRef.current;
        const current = () => mountedRef.current && ownsIdentity(requestedOwner) && submitGenerationRef.current === generation;
        setSubmitting(kind);
        void createDataRequest(kind)
          .then((created) => {
            if (current()) {
              setItems((previous) => [created, ...(previous ?? []).filter(item => item.id !== created.id)]);
              showAlert("Request received", "Return here to check its status. Keep the request reference if you contact support.");
            }
          })
          .catch((error) => {
            if (!current()) return;
            if (error instanceof ApiError && error.detail === "request_already_open") {
              showAlert("Request already open", "A request of this type is already being handled. Refresh status to see it.");
            } else {
              showRequestError(error, "Couldn't submit your request");
            }
          })
          .finally(() => {
            if (current()) {
              submitBusyRef.current = false;
              setSubmitting(null);
            }
          });
      },
    });
  };

  return (
    <Screen title="Your data" subtitle="Request a copy or account deletion" onRefresh={load}>
      <View style={{ gap: spacing.lg }}>
        <Card>
          <AppText variant="headline">Account data requests</AppText>
          <AppText variant="body" tone="secondary" style={{ marginTop: spacing.sm }}>
            Request data for your signed-in account. Deletion requires review, and some financial or safety records may need to be kept. We explain any remaining records in your request status.
          </AppText>
          <View style={{ gap: spacing.sm, marginTop: spacing.lg }}>
            <Button label="Request a data copy" onPress={() => submit("export")} disabled={submitting !== null} />
            <Button label="Request account deletion" variant="destructive" onPress={() => submit("deletion")} disabled={submitting !== null} />
          </View>
        </Card>
        {loading && items === null ? <AppText variant="caption" tone="secondary">Loading request history…</AppText> : null}
        {items !== null && items.length === 0 ? (
          <EmptyState title="No requests yet" message="Your request history will appear here." />
        ) : null}
        {items?.map((item) => <RequestCard key={`${owner.generation}:${item.id}`} item={item} owner={owner} onRefresh={() => void load()} />)}
        <Pressable onPress={() => void openLegalLink("Data requests", "https://chirpsocials.com/data-requests")} accessibilityRole="link" style={{ minHeight: 44, justifyContent: "center" }}>
          <AppText variant="caption" tone="accent" style={{ textAlign: "center" }}>
            Read the data request policy
          </AppText>
        </Pressable>
      </View>
    </Screen>
  );
}
