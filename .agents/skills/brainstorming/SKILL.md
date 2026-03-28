---
name: brainstorming
description: Create and manage brainstorming documents for GitHub issues. Use when exploring ideas, planning features, or documenting decisions before creating formal specs.
license: MIT
compatibility: Requires gh CLI (GitHub CLI)
metadata:
  author: FnSK4R17s
  version: "1.2"
---

# Brainstorming

Create and manage brainstorming documents linked to GitHub issues. Brainstorming docs are **ephemeral** - they become formal specs when ready for implementation, then can be discarded.

## Guiding Documents

Unlike brainstorming, **guiding docs** are permanent reference material that shape all project decisions:

| Location | Purpose |
|----------|---------|
| `guiding_docs/VISION.md` | Gateway architecture, security model, tech stack, scope |

**Always read `guiding_docs/VISION.md` first** before starting any brainstorm. New features should align with:
- Three security layers: phantom tokens, dual-layer RBAC, stateless sessions
- Tech stack: Python 3.12+, FastAPI, FastMCP, Cerbos, Redis
- Non-goals: not reimplementing MCP protocol, not agent orchestration (that's commandclaw)

When brainstorming produces lasting insights, promote them to guiding docs rather than keeping them in `brainstorming/`.

## Overview

```
GitHub Issue #12 <-> brainstorming/issue-012-session-pooling/
                         |- notes.md      (from brainstorming skill templates/notes.md)
                         |- tasks.md      (optional, from brainstorm-to-tasks skill templates/tasks.md)
                         +- research.md   (optional extras)
```

## Pre-Brainstorm Checklist

Before creating a brainstorm document, ask the user:

### 1. Does a GitHub issue exist?

If not, offer to create one:

```bash
gh issue create --title "Feature: <title>" --body "Brainstorming needed for <description>" --label "brainstorm"
```

This gives the brainstorm a trackable home. Capture the issue number for the brainstorm folder.

### 2. Should we create a feature branch?

Ask: *"Would you like a dedicated branch for this brainstorm? This keeps exploration isolated from main."*

If yes:

```bash
ISSUE_NUM=12
SLUG="session-pooling"
git checkout -b brainstorm/issue-${ISSUE_NUM}-${SLUG}
```

Branch naming convention: `brainstorm/issue-{NNN}-{slug}`

When the brainstorm matures into implementation, the branch can be rebased or a new `feature/` branch created from it.

## Quick Start

### Create New Brainstorm from Issue

```bash
# 1. Get issue details
ISSUE_NUM=12
TITLE=$(gh issue view $ISSUE_NUM --json title --jq '.title' | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g')

# 2. Create from template
FOLDER="brainstorming/issue-$(printf '%03d' $ISSUE_NUM)-${TITLE:0:30}"
mkdir -p "$FOLDER"
cp .agents/skills/brainstorming/templates/notes.md "$FOLDER/notes.md"

# 3. Fill in placeholders
FULL_TITLE=$(gh issue view $ISSUE_NUM --json title --jq '.title')
sed -i "s/{{ISSUE_NUMBER}}/$ISSUE_NUM/g" "$FOLDER/notes.md"
sed -i "s/{{TITLE}}/$FULL_TITLE/g" "$FOLDER/notes.md"
sed -i "s/{{DATE}}/$(date +%Y-%m-%d)/g" "$FOLDER/notes.md"

echo "Created: $FOLDER/notes.md"
```

### Create Brainstorm Without an Existing Issue

If the user wants to brainstorm first and create an issue later:

```bash
SLUG="credential-encryption"
FOLDER="brainstorming/${SLUG}"
mkdir -p "$FOLDER"
cp .agents/skills/brainstorming/templates/notes.md "$FOLDER/notes.md"
sed -i "s/{{ISSUE_NUMBER}}/TBD/g; s/{{TITLE}}/${SLUG}/g; s/{{DATE}}/$(date +%Y-%m-%d)/g" "$FOLDER/notes.md"
```

When ready, create the issue and link it:

```bash
gh issue create --title "Feature: Credential Encryption" --body "See brainstorming/${SLUG}/notes.md"
```

### Add Tasks (Optional)

When you need to break down implementation:

```bash
ISSUE_NUM=12
FOLDER=$(ls -d brainstorming/issue-$(printf '%03d' $ISSUE_NUM)-* 2>/dev/null | head -1)
cp .agents/skills/brainstorm-to-tasks/templates/tasks.md "$FOLDER/tasks.md"
sed -i "s/{{ISSUE_NUMBER}}/$ISSUE_NUM/g; s/{{DATE}}/$(date +%Y-%m-%d)/g" "$FOLDER/tasks.md"
echo "Created: $FOLDER/tasks.md"
```

## Commands

### List All Brainstorms

```bash
ls -la brainstorming/
```

### Find Brainstorm for Issue

```bash
ls brainstorming/ | grep "issue-012"
```

### Check Brainstorm Status

```bash
grep -h "^\*\*Status\*\*:" brainstorming/*/notes.md 2>/dev/null
```

### List Issues with Brainstorms

```bash
for dir in brainstorming/issue-*/; do
  num=$(echo "$dir" | grep -oE '[0-9]+' | head -1)
  status=$(grep "^\*\*Status\*\*:" "$dir/notes.md" 2>/dev/null | head -1)
  echo "#$num: $status"
done
```

### Check Issue Status from GitHub

```bash
ISSUE_NUM=12
gh issue view $ISSUE_NUM --json state,title --jq '"\(.state): \(.title)"'
```

## Folder Naming Convention

```
brainstorming/
|- issue-001-phantom-token-rotation/
|- issue-004-cerbos-integration/
|- issue-012-session-pooling/
+- credential-encryption/              # No issue link (exploratory)
```

**Format**: `issue-{NNN}-{slug}/`
- `NNN`: Zero-padded issue number (for sorting)
- `slug`: Lowercase, hyphenated title excerpt

## Template Placeholders

| Placeholder | Replaced With |
|-------------|---------------|
| `{{ISSUE_NUMBER}}` | GitHub issue number |
| `{{TITLE}}` | Issue title |
| `{{DATE}}` | Creation date (YYYY-MM-DD) |

## Workflow

```
0. (Optional) Create GitHub issue if none exists
        |
1. (Optional) Create brainstorm branch: brainstorm/issue-NNN-slug
        |
2. Brainstorm Created (brainstorming/issue-012-*)
        |
3. Ideas Explored, Decisions Made
        |
4. Status -> "Ready for Implementation"
        |
5. brainstorm-to-tasks skill generates tasks.md
        |
6. (Optional) Create feature branch from brainstorm branch
        |
7. implement-tasks skill executes the work
        |
8. Brainstorm Status -> "Archived"
```

## Best Practices

1. **One brainstorm per issue** - Keep them linked
2. **Update status** - Draft -> In Progress -> Ready for Implementation -> Archived
3. **Link to issue** - Always include the issue link in frontmatter
4. **Archive don't delete** - Mark as archived when implementation begins
5. **Use `gh` CLI** - All GitHub operations go through `gh`, not raw API calls

## Templates

| Template | Purpose |
|----------|--------|
| `.agents/skills/brainstorming/templates/notes.md` | Main brainstorm document - ideas, decisions, approach |
| `.agents/skills/brainstorm-to-tasks/templates/tasks.md` | Task breakdown - phases, estimates, acceptance criteria |

Customize them for your project's needs.
