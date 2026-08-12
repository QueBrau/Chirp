/** Entry redirect: signed-in users land on the Feed tab, everyone else on sign-in. */

import { Redirect } from "expo-router";

import { IS_SIGNED_IN } from "./_layout";

export default function Index() {
  return <Redirect href={IS_SIGNED_IN ? "/feed" : "/sign-in"} />;
}
