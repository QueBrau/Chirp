# Chirp — agent instructions

See **CLAUDE.md** at the repo root. It is the single instruction file for every coding
agent on this project (Claude Code reads CLAUDE.md, Cursor reads this file) — keeping
two copies means one of them is wrong within a week.

The part that gets skipped most: **board.html is updated at every step, not at the end
of a task, and board commits go straight to main so the other dev is never in the dark.**
