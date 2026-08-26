# c174 — Expo SDK 54 -> 57 upgrade: rollback recipe

The commit that adds this file is the pre-upgrade baseline, taken before any
dependency change on this branch. Find it with:

```
git log --oneline -- app-mobile/C174-UPGRADE-ROLLBACK.md | tail -1
```

That commit's `package.json` / `package-lock.json` are the last known-good
Expo 54 tree. To roll back the app-mobile dependency tree to pre-c174 state
from any later commit on this branch:

```
BASELINE=$(git log --format=%H -- app-mobile/C174-UPGRADE-ROLLBACK.md | tail -1)
git checkout "$BASELINE" -- app-mobile/package.json app-mobile/package-lock.json
cd app-mobile && rm -rf node_modules
npm ci
```

Then re-verify with `npx tsc --noEmit` and the `verify:*` scripts in
`package.json` before trusting the rolled-back tree.

See board.html card c174 for the full upgrade plan and c170 for the source
npm-audit findings this upgrade clears.
