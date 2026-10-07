import { request } from "./client";

export interface OrganizationAuthority {
  policy_version: string;
  confirmed: true;
}
export interface PaymentAuthorityContext {
  policy_version: string;
  chapter_id: string;
  org_name: string;
  membership_id: string;
  role_term_id: string;
  role: string;
  stripe_account_id: string | null;
}
export type PaymentAuthority = OrganizationAuthority & Pick<PaymentAuthorityContext,
  "membership_id" | "role_term_id" | "role" | "stripe_account_id">;

export function getOrganizationPolicy(): Promise<{ version: string; url: string }> {
  return request("/legal/organization-policy");
}
export function getPaymentAuthority(chapterId: string): Promise<PaymentAuthorityContext> {
  return request(`/chapters/${chapterId}/payments/authority`);
}
export function paymentDeclaration(context: PaymentAuthorityContext): PaymentAuthority {
  return { policy_version: context.policy_version, confirmed: true,
    membership_id: context.membership_id, role_term_id: context.role_term_id,
    role: context.role, stripe_account_id: context.stripe_account_id };
}
