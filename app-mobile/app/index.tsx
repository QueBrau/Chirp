/** Entry redirect: always land on the Feed tab — the (tabs) auth guard bounces signed-out users to sign-in. */

import { Redirect } from "expo-router";

export default function Index() {
  return <Redirect href="/feed" />;
}
