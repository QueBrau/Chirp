import { Linking } from "react-native";

import { showAlert } from "./alert";

export const TERMS_URL = "https://chirpsocials.com/terms";
export const PRIVACY_URL = "https://chirpsocials.com/privacy";
export const SUPPORT_URL = "https://chirpsocials.com/contact";

export async function openLegalLink(label: string, url: string): Promise<void> {
  try {
    await Linking.openURL(url);
  } catch {
    showAlert(`Couldn't open ${label}`, `Open this address in your browser: ${url}`);
  }
}
