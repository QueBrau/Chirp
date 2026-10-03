/**
 * Regression trap for the c423 alumni editor.
 *
 *   npm run verify:c423-alumni
 *
 * c423 was "the Add your company / Add your class year rows do nothing when
 * tapped". The fix is two parts — rows that navigate, and an editor to navigate
 * to — and the dangerous half is the editor, because of how its endpoint
 * behaves.
 *
 * THE PROPERTY THIS FILE EXISTS FOR. `PUT /alumni/profile`
 * (backend/app/routers/alumni.py upsert_own_profile) assigns EVERY column from
 * the request body unconditionally, and every field on `AlumniProfileUpdate`
 * defaults to None/False. So a partial body is not a partial update — it is a
 * silent erase of everything it omits. An editor that sends six of seven fields
 * NULLs the seventh, and nothing about that failure is visible at the call site,
 * in a typecheck, or on the screen that submitted it. The owner finds out later
 * when their class year is gone.
 *
 * That is why check 2 does not hard-code the seven field names. It reads them
 * out of `AlumniProfileUpdate` in src/api/alumni.ts and asserts the submitted
 * object's key set EQUALS that interface's field set. A field added to the API
 * type tomorrow therefore fails this check until the editor sends it too, which
 * is the only version of this assertion that keeps working after I am gone. A
 * hard-coded list of seven would have gone stale at the exact moment it mattered.
 *
 * ASSERTIONS ARE COMPUTED OR POSITIONAL, NEVER "the string is present", per
 * verify-failure-states.mjs's rule — a presence check passes on a file where the
 * thing exists but is unreachable, which is the failure mode itself. Every
 * anchor below is syntax, never prose.
 *
 * HOW TO FALSIFY THIS FILE. Restore the real prior state rather than writing a
 * stand-in:
 *   git show origin/main:app-mobile/app/\(tabs\)/profile/index.tsx > app/\(tabs\)/profile/index.tsx
 *   npm run verify:c423-alumni    # checks 6, 7 and 8 must FAIL
 *   git checkout app/\(tabs\)/profile/index.tsx
 * A hand-built imitation of the bug is one you wrote to be caught. And read the
 * offender lines, not the summary count: a green sabotage run means either the
 * assertion is weak or the sabotage never landed, and only one of those is good.
 */
import { readFileSync } from "node:fs";

const read = (rel) => readFileSync(new URL(`../${rel}`, import.meta.url), "utf8");

const PROFILE = "app/(tabs)/profile/index.tsx";
const EDITOR = "app/(tabs)/profile/alumni-info.tsx";
const API = "src/api/alumni.ts";

const sources = { [PROFILE]: read(PROFILE), [EDITOR]: read(EDITOR), [API]: read(API) };

let failures = 0;
const pass = (name) => console.log(`  PASS  ${name}`);
const fail = (name, detail) => {
  console.log(`  FAIL  ${name}`);
  if (detail) console.log(`        ${detail}`);
  failures++;
};

/** Index of an exact syntax anchor, or -1 with a recorded failure. */
function at(file, anchor, why) {
  const idx = sources[file].indexOf(anchor);
  if (idx === -1) fail(`anchor missing in ${file}: ${anchor}`, why);
  return idx;
}

/**
 * The balanced `{...}` or `(...)` span starting at the first opener at/after
 * `from`. Brace counting rather than a regex because every span below nests,
 * and a regex would stop at the first inner closer and silently under-read —
 * which would make the key-set checks pass on a truncated object.
 */
function span(text, from, open = "{", close = "}") {
  const start = text.indexOf(open, from);
  if (start === -1) return null;
  let depth = 0;
  for (let i = start; i < text.length; i++) {
    if (text[i] === open) depth++;
    else if (text[i] === close) {
      depth--;
      if (depth === 0) return { start, end: i, text: text.slice(start, i + 1) };
    }
  }
  return null;
}

const sameSet = (a, b) => a.length === b.length && [...a].sort().join() === [...b].sort().join();

// ---------------------------------------------------------------------------
// 1. The API type still has the shape the rest of this file reasons about.
// ---------------------------------------------------------------------------
const api = sources[API];
const ifaceIdx = at(API, "export interface AlumniProfileUpdate {", "the PUT body type is gone or renamed");
const ifaceBody = ifaceIdx === -1 ? null : span(api, ifaceIdx);
/** Top-level members only: two-space indent, then `name?:` or `name:`. */
const apiFields = ifaceBody
  ? [...ifaceBody.text.matchAll(/^ {2}(\w+)\??:/gm)].map((m) => m[1])
  : [];
if (apiFields.length === 0) {
  fail("AlumniProfileUpdate field extraction", "parsed zero fields — the parser, not the code, is wrong");
} else {
  pass(`AlumniProfileUpdate exposes ${apiFields.length} fields: ${apiFields.join(", ")}`);
}

// ---------------------------------------------------------------------------
// 2. THE ONE THAT MATTERS. The editor submits every field the type declares.
//    Full replace: an omitted field is erased, not preserved.
// ---------------------------------------------------------------------------
const editor = sources[EDITOR];
const callIdx = at(EDITOR, "await updateAlumniProfile(", "the editor does not call the update API at all");
const submitted = callIdx === -1 ? null : span(editor, callIdx);
const sentKeys = submitted ? [...submitted.text.matchAll(/^ {8}(\w+):/gm)].map((m) => m[1]) : [];

if (!submitted || sentKeys.length === 0) {
  fail("submitted key extraction", "found no keys in the updateAlumniProfile body");
} else if (!sameSet(sentKeys, apiFields)) {
  const missing = apiFields.filter((f) => !sentKeys.includes(f));
  const extra = sentKeys.filter((f) => !apiFields.includes(f));
  fail(
    "editor submits every AlumniProfileUpdate field",
    `PUT /alumni/profile is a FULL REPLACE, so omitted fields are ERASED.` +
      (missing.length ? ` Missing (would be wiped on save): ${missing.join(", ")}.` : "") +
      (extra.length ? ` Not on the API type: ${extra.join(", ")}.` : ""),
  );
} else {
  pass(`editor submits all ${sentKeys.length} fields — nothing gets wiped by omission`);
}

// ---------------------------------------------------------------------------
// 3. Blank text becomes null, never "". The read paths all use `?? "Add your…"`,
//    which an empty string passes — so "" renders a filled row holding nothing.
// ---------------------------------------------------------------------------
if (submitted) {
  const stringFields = ["company", "title", "industry", "location", "linkedin_url"];
  const unnormalized = stringFields.filter((f) => {
    const m = new RegExp(`^ {8}${f}: (.*)$`, "m").exec(submitted.text);
    return m === null || !m[1].includes("trimmedOrNull(");
  });
  if (unnormalized.length > 0) {
    fail(
      "blank text fields normalize to null",
      `these submit a raw string, so "" would store as empty-not-absent: ${unnormalized.join(", ")}`,
    );
  } else {
    pass("every text field submits through trimmedOrNull — blank means null");
  }
}

// ---------------------------------------------------------------------------
// 4. A failed LOAD must not render the form. Positional: the guard precedes the
//    first input. This is c319's defect in a new place — blank fields shown to
//    someone whose profile is full, then saved over it by the full replace.
// ---------------------------------------------------------------------------
const guardIdx = at(EDITOR, "if (loadFailed) {", "no load-failure branch — a dropped request opens a blank form");
const firstInputIdx = editor.indexOf("<TextInput");
if (guardIdx !== -1 && firstInputIdx !== -1) {
  if (guardIdx < firstInputIdx) {
    pass("load failure returns before any TextInput renders");
  } else {
    fail(
      "load failure returns before any TextInput renders",
      `guard at ${guardIdx} comes AFTER the first input at ${firstInputIdx} — the form is reachable on a failed read`,
    );
  }
}

// ---------------------------------------------------------------------------
// 5. A failed SAVE keeps every keystroke. Computed over the save catch block:
//    it must not write to form state.
// ---------------------------------------------------------------------------
if (callIdx !== -1) {
  const catchIdx = editor.indexOf("} catch (err) {", callIdx);
  const catchBody = catchIdx === -1 ? null : span(editor, catchIdx + "} catch (err)".length);
  if (!catchBody) {
    fail("save failure preserves input", "could not locate the save catch block");
  } else if (catchBody.text.includes("setForm(")) {
    fail("save failure preserves input", "the save catch writes form state — a failed save would discard typing");
  } else {
    pass("save failure does not touch form state — input survives");
  }
}

// ---------------------------------------------------------------------------
// 6. All three rows navigate. Computed over the alumni branch: every ListRow in
//    it carries an onPress. A fourth row added without one fails this.
// ---------------------------------------------------------------------------
const profile = sources[PROFILE];
const alumniStart = at(PROFILE, '{section.key === "alumni" ?', "the alumni section branch is gone");
const settingsStart = profile.indexOf('{section.key === "settings" ?', alumniStart === -1 ? 0 : alumniStart);
if (alumniStart !== -1 && settingsStart > alumniStart) {
  const region = profile.slice(alumniStart, settingsStart);
  const rows = (region.match(/<ListRow\b/g) ?? []).length;
  const pressable = (region.match(/\bonPress=/g) ?? []).length;
  if (rows === 0) {
    fail("alumni rows are tappable", "found no ListRow in the alumni section");
  } else if (rows !== pressable) {
    fail(
      "alumni rows are tappable",
      `${rows} ListRow(s) in the alumni section but ${pressable} onPress — ${rows - pressable} row(s) still invite an edit that cannot happen (the c423 bug)`,
    );
  } else {
    pass(`all ${rows} alumni rows carry onPress`);
  }
} else {
  fail("alumni section bounds", "could not bound the alumni branch against the settings branch");
}

// ---------------------------------------------------------------------------
// 7. The profile screen refetches on FOCUS. A mount-only effect leaves the rows
//    stale after the editor pops back, which reads as a save that did nothing.
// ---------------------------------------------------------------------------
const focusIdx = at(PROFILE, "useFocusEffect(", "no focus effect — saved values would not appear until a cold start");
if (focusIdx !== -1) {
  const focusSpan = span(profile, focusIdx, "(", ")");
  if (focusSpan && focusSpan.text.includes("loadAlumniProfile()")) {
    pass("loadAlumniProfile runs inside useFocusEffect");
  } else {
    fail(
      "loadAlumniProfile runs inside useFocusEffect",
      "the alumni loader is not called within the focus effect's own argument span",
    );
  }
}

// ---------------------------------------------------------------------------
// 8. The pushed route matches the editor's real filename. Derived from the file
//    path, so renaming the screen without updating the push fails here instead
//    of at runtime on a phone.
// ---------------------------------------------------------------------------
const expectedRoute = `/profile/${EDITOR.split("/").pop().replace(/\.tsx$/, "")}`;
if (profile.includes(`router.push("${expectedRoute}")`)) {
  pass(`profile pushes ${expectedRoute}, matching the editor's filename`);
} else {
  fail(
    `profile pushes ${expectedRoute}`,
    `no router.push("${expectedRoute}") in ${PROFILE} — the rows navigate somewhere the route tree has no file for`,
  );
}

// ---------------------------------------------------------------------------
// 9. The POST-SAVE navigation is guarded by the focus that submitted it.
//    c432, pinning Codex's 9122e17 review fix. The editor used to call
//    router.back() unconditionally on success, so a save still in flight when
//    the user left popped whichever screen they had moved to instead.
//
//    Targeted positionally, not globally: there are THREE router.back() calls
//    in this file and only this one may be guarded. The Back button in the
//    load-failure state and the Cancel button are user-initiated and must fire
//    unconditionally, so a blanket "every router.back() is guarded" check would
//    demand the wrong thing.
// ---------------------------------------------------------------------------
const submitIdx = editor.indexOf("await updateAlumniProfile(");
const saveCatchIdx = editor.indexOf("} catch (err) {", submitIdx);
if (submitIdx === -1 || saveCatchIdx === -1) {
  fail("post-save navigation is focus-guarded", "could not bound the save success path");
} else {
  const successPath = editor.slice(submitIdx, saveCatchIdx);
  const navLine = successPath.split("\n").find((l) => l.includes("router.back()"));
  if (navLine === undefined) {
    fail("post-save navigation is focus-guarded", "no router.back() in the save success path at all");
  } else if (!/activeFocus\.current === submittingFocus/.test(navLine)) {
    fail(
      "post-save navigation is focus-guarded",
      `the success path navigates unconditionally — a save resolving after the user leaves pops their current screen. Line: ${navLine.trim()}`,
    );
  } else {
    pass("post-save navigation only fires for the focus that submitted it");
  }
}

// ---------------------------------------------------------------------------
// 10. The guard can actually TRIP. Two ways it silently cannot, both checked:
//     the identity must be captured BEFORE the await (comparing a value read
//     after the await against itself is always true), and the focus effect must
//     CLEAR it on blur (without the cleanup, `current` keeps the same identity
//     forever and the comparison can never be false).
// ---------------------------------------------------------------------------
const captureIdx = editor.indexOf("const submittingFocus = activeFocus.current;");
if (captureIdx === -1) {
  fail("the focus guard can trip", "no captured focus identity — nothing to compare against");
} else if (submitIdx !== -1 && captureIdx > submitIdx) {
  fail(
    "the focus guard can trip",
    "the identity is captured AFTER the await, so the comparison comes out true no matter what happened",
  );
} else {
  const focusEffectIdx = editor.indexOf("useFocusEffect(");
  const effect = focusEffectIdx === -1 ? null : span(editor, focusEffectIdx, "(", ")");
  const clears = effect !== null && /return \(\) =>[\s\S]*activeFocus\.current = null/.test(effect.text);
  if (!clears) {
    fail(
      "the focus guard can trip",
      "the focus effect never clears activeFocus on blur, so its identity never changes and the guard is inert",
    );
  } else {
    pass("focus identity is captured before the await and cleared on blur");
  }
}

// ---------------------------------------------------------------------------
// 11. Every input is frozen while saving. Counted rather than spot-checked: the
//     submit reads `form` at call time, so a field still editable mid-save lets
//     someone type edits that look saved and are not. A seventh input added
//     without the guard fails this.
// ---------------------------------------------------------------------------
const inputCount = (editor.match(/<TextInput\b/g) ?? []).length;
const editableGuards = (editor.match(/editable=\{!saving\}/g) ?? []).length;
if (inputCount === 0) {
  fail("inputs freeze while saving", "found no TextInput in the editor");
} else if (inputCount !== editableGuards) {
  fail(
    "inputs freeze while saving",
    `${inputCount} TextInput(s) but ${editableGuards} editable={!saving} — ${inputCount - editableGuards} stay editable mid-save, so edits made then are silently dropped`,
  );
} else {
  pass(`all ${inputCount} inputs freeze while saving`);
}

// ---------------------------------------------------------------------------
// 12. The mentoring toggle is inert while saving, for the same reason: it is the
//     one field that is not a TextInput, so check 11 cannot see it.
// ---------------------------------------------------------------------------
const toggleIdx = editor.indexOf('title="Open to mentoring"');
const toggleRow = toggleIdx === -1 ? null : editor.slice(toggleIdx, toggleIdx + 700);
if (toggleRow === null) {
  fail("mentoring toggle is inert while saving", "could not find the toggle row");
} else {
  const onPress = /onPress=\{([^\n]*)\}/.exec(toggleRow);
  if (onPress === null || !onPress[1].includes("saving")) {
    fail(
      "mentoring toggle is inert while saving",
      "the toggle's onPress does not consult `saving`, so it can change a value the in-flight submit has already read",
    );
  } else {
    pass("mentoring toggle consults saving before changing the value");
  }
}

console.log(
  failures === 0
    ? "\nverify:c423-alumni — all checks passed"
    : `\nverify:c423-alumni — ${failures} check(s) failed`,
);
process.exit(failures === 0 ? 0 : 1);
