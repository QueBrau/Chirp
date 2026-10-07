/** Account export and deletion request status. */

import { Feather } from "@expo/vector-icons";
import { useCallback, useEffect, useState } from "react";
import { Linking, Pressable, View } from "react-native";

import {
  createDataRequest,
  listDataRequests,
  type DataRequestKind,
  type DataRequestOut,
} from "@/api/dataRequests";
import { ApiError } from "@/api/client";
import { AppText, Button, Card, EmptyState, Screen } from "@/components";
import { confirmAction, showAlert, showApiError } from "@/lib/alert";
import { radii, spacing, useTheme } from "@/theme";

function statusLabel(status: DataRequestOut["status"]): string {
  return status.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function RequestCard({ item, onRefresh }: { item: DataRequestOut; onRefresh: () => void }) {
  const palette = useTheme();
  const isExport = item.kind === "export";
  const canDownload = isExport && item.status === "ready" && item.download_url !== null;

  const download = async () => {
    if (!item.download_url) return;
    try {
      await Linking.openURL(item.download_url);
    } catch {
      showAlert("Couldn't open export", "Refresh the request and try again, or contact support.");
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
          Needs separate handling: {item.excluded.join(", ")}.
        </AppText>
      ) : null}
      {item.retention_reasons.length > 0 ? (
        <AppText variant="caption" tone="secondary" style={{ marginTop: spacing.sm }}>
          Some records may remain where required for financial, safety, shared organization, or legal obligations.
        </AppText>
      ) : null}
      {item.failure_code ? (
        <AppText variant="caption" tone="danger" style={{ marginTop: spacing.sm }}>
          This request needs support review. Reference: {item.id}.
        </AppText>
      ) : null}
      <View style={{ flexDirection: "row", gap: spacing.sm, marginTop: spacing.md }}>
        {canDownload ? <Button label="Download copy" onPress={() => void download()} /> : null}
        <Button label="Refresh status" variant="neutral" onPress={onRefresh} />
      </View>
    </Card>
  );
}

export default function AccountDataScreen() {
  const [items, setItems] = useState<DataRequestOut[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState<DataRequestKind | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await listDataRequests());
    } catch (error) {
      setItems(null);
      showApiError(error, "Couldn't load your data requests");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const submit = (kind: DataRequestKind) => {
    const isDeletion = kind === "deletion";
    confirmAction({
      title: isDeletion ? "Request account deletion?" : "Request a copy of your data?",
      message: isDeletion
        ? "This starts a review of your account and associated records. It does not erase shared, financial, safety, or legally required records automatically. You can contact support about the scope before fulfillment."
        : "We will prepare the account data currently available for export. Records held by payment, authentication, email, storage, or other providers may need separate handling.",
      confirmLabel: isDeletion ? "Request deletion" : "Request copy",
      destructive: isDeletion,
      onConfirm: () => {
        setSubmitting(kind);
        void createDataRequest(kind)
          .then((created) => {
            setItems((previous) => [created, ...(previous ?? [])]);
            showAlert("Request received", "You can return here to check its status. We may contact you to verify the request.");
          })
          .catch((error) => {
            if (error instanceof ApiError && error.detail === "request_already_open") {
              showAlert("Request already open", "A request of this type is already being handled. Refresh status to see it.");
            } else {
              showApiError(error, "Couldn't submit your request");
            }
          })
          .finally(() => setSubmitting(null));
      },
    });
  };

  return (
    <Screen title="Your data" subtitle="Request a copy or account deletion" onRefresh={load}>
      <View style={{ gap: spacing.lg }}>
        <Card>
          <AppText variant="headline">Account data requests</AppText>
          <AppText variant="body" tone="secondary" style={{ marginTop: spacing.sm }}>
            These requests are authenticated to your current account. A request starts scoped review and fulfillment; it does not promise that every provider copy or shared record can be erased.
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
        {items?.map((item) => <RequestCard key={item.id} item={item} onRefresh={() => void load()} />)}
        <Pressable onPress={() => void Linking.openURL("https://chirpsocials.com/data-requests")} accessibilityRole="link">
          <AppText variant="caption" tone="accent" style={{ textAlign: "center" }}>
            Read the data request policy
          </AppText>
        </Pressable>
      </View>
    </Screen>
  );
}
