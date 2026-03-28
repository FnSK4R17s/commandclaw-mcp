---
name: bugfix
description: Create structured bugfix documents for tracking bug investigations, root cause analysis, and fixes. Use when a bug is reported via GitHub issue, user report, or agent observation.
license: MIT
compatibility: Requires curl and basic shell
metadata:
  author: FnSK4R17s
  version: "1.0"
---

# Bugfix

Create structured bugfix documents that track the full lifecycle: report, reproduction, root cause analysis, fix, and verification. Bugfix docs live alongside brainstorming docs and follow the same issue-linking convention.

## Overview

```
GitHub Issue #7 <-> brainstorming/issue-007-hmac-timing-leak/
                        |- notes.md       (brainstorm, if exploration needed)
                        |- bugfix01.md    (first bug in this area)
                        |- bugfix02.md    (second bug, if multiple)
                        +- tasks.md       (if fix requires multiple tasks)
```

For standalone bugs without a brainstorming context:

```
brainstorming/issue-007-hmac-timing-leak/
    +- bugfix01.md    (can exist without notes.md)
```

## Quick Start

### Create Bugfix from Issue

```bash
ISSUE_NUM=7
REPO=$(git remote get-url origin | sed 's/.*github.com[:/]\(.*\)\.git/\1/')
ISSUE=$(curl -s "https://api.github.com/repos/$REPO/issues/$ISSUE_NUM")
TITLE=$(echo "$ISSUE" | jq -r '.title' | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g')

FOLDER="brainstorming/issue-$(printf '%03d' $ISSUE_NUM)-${TITLE:0:30}"
mkdir -p "$FOLDER"

# Find next bugfix number
NEXT=$(ls "$FOLDER"/bugfix*.md 2>/dev/null | wc -l)
NEXT=$((NEXT + 1))
BUGFIX_FILE="$FOLDER/bugfix$(printf '%02d' $NEXT).md"

# Copy from template and fill placeholders
cp .templates/bugfix.md "$BUGFIX_FILE"
sed -i "s/{{BUGFIX_NUM}}/$NEXT/g; s/{{ISSUE_NUMBER}}/$ISSUE_NUM/g; s/{{DATE}}/$(date +%Y-%m-%d)/g" "$BUGFIX_FILE"
sed -i "s/{{TITLE}}/$(echo "$ISSUE" | jq -r '.title')/g" "$BUGFIX_FILE"

echo "Created: $BUGFIX_FILE"
```

### Create Bugfix Without an Issue

For bugs discovered during development (not from a GitHub issue):

```bash
SLUG="hmac-timing-leak"
FOLDER="brainstorming/bug-${SLUG}"
mkdir -p "$FOLDER"

# Same template, just without the issue link
```

## Bugfix Template Fields

| Field | Purpose |
|-------|---------|
| **Symptom** | What the user/agent sees -- error messages, wrong behavior, crashes |
| **Reproduction** | Exact steps to trigger the bug, including config and inputs |
| **Root Cause** | The actual code defect -- module, function, logic error |
| **Fix** | What was changed, which files, and why this approach |
| **Verification** | Checklist proving the fix works and doesn't regress |
| **Lessons** | What to do differently -- update conventions, add tests, improve validation |

## Status Values

| Status | Meaning |
|--------|---------|
| Investigating | Bug reported, reproduction not confirmed |
| Reproduced | Bug confirmed, root cause unknown |
| Root Caused | Root cause identified, fix not started |
| Fix In Progress | Actively writing the fix |
| Verification | Fix written, running tests |
| Resolved | Fix verified, tests pass |

## Severity Definitions

| Severity | Meaning | Examples |
|----------|---------|---------|
| P0 | Security / data loss | Credential leak, token bypass, data corruption |
| P1 | Broken functionality | Tool calls fail, auth rejects valid tokens, session loss |
| P2 | Degraded experience | Slow rotation, noisy logs, misleading errors |
| P3 | Minor | Cosmetic, non-blocking edge case |

**P0 bugs in auth/, rbac/, or credential_store.py get immediate attention.** A security bug in the gateway is a security bug for every agent behind it.

## Integration with implement-tasks

Once a bugfix has a clear root cause and fix approach, the `implement-tasks` skill applies:

1. Read `guiding_docs/VISION.md` (always)
2. Read the bugfix document for context
3. Check cross-module impact (Step 5 of implement-tasks)
4. Apply the fix following project conventions
5. Run tests and update the bugfix verification checklist
6. Mark status as Resolved

For complex bugs requiring multiple changes, generate a `tasks.md` using the `brainstorm-to-tasks` skill with the bugfix document as input.

## Best Practices

1. **Write the reproduction steps first** -- if you can't reproduce it, you can't verify the fix
2. **One bug per bugfix file** -- don't combine unrelated bugs
3. **Link the root cause to specific code** -- module, function, line if possible
4. **Fill in Lessons** -- this is how the project's conventions and tests improve over time
5. **P0 security bugs** -- check if the vulnerability exists in other similar code paths

## Related Skills

| Skill | Relationship |
|-------|--------------|
| `brainstorming` | Bugfixes can live inside brainstorming folders |
| `brainstorm-to-tasks` | Complex bugs can be broken into task lists |
| `implement-tasks` | Executes the actual fix with pre-flight checks |
