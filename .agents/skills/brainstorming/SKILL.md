---
name: brainstorming
description: Create and manage brainstorming documents for GitHub issues. Use when exploring ideas, planning features, or documenting decisions before creating formal specs.
license: MIT
compatibility: Requires curl and basic shell
metadata:
  author: FnSK4R17s
  version: "1.1"
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

## Quick Start

### Create New Brainstorm from Issue

```bash
# 1. Get issue details
ISSUE_NUM=12
REPO=$(git remote get-url origin | sed 's/.*github.com[:/]\(.*\)\.git/\1/')
ISSUE=$(curl -s "https://api.github.com/repos/$REPO/issues/$ISSUE_NUM")
TITLE=$(echo "$ISSUE" | jq -r '.title' | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g')

# 2. Create from template
FOLDER="brainstorming/issue-$(printf '%03d' $ISSUE_NUM)-${TITLE:0:30}"
mkdir -p "$FOLDER"
cp .agents/skills/brainstorming/templates/notes.md "$FOLDER/notes.md"

# 3. Fill in placeholders
sed -i "s/{{ISSUE_NUMBER}}/$ISSUE_NUM/g" "$FOLDER/notes.md"
sed -i "s/{{TITLE}}/$(echo "$ISSUE" | jq -r '.title')/g" "$FOLDER/notes.md"
sed -i "s/{{DATE}}/$(date +%Y-%m-%d)/g" "$FOLDER/notes.md"

echo "Created: $FOLDER/notes.md"
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
1. Issue Created (#12)
        |
2. Brainstorm Created (brainstorming/issue-012-*)
        |
3. Ideas Explored, Decisions Made
        |
4. Status -> "Ready for Implementation"
        |
5. brainstorm-to-tasks skill generates tasks.md
        |
6. implement-tasks skill executes the work
        |
7. Brainstorm Status -> "Archived"
```

## Best Practices

1. **One brainstorm per issue** - Keep them linked
2. **Update status** - Draft -> In Progress -> Ready for Implementation -> Archived
3. **Link to issue** - Always include the issue link in frontmatter
4. **Archive don't delete** - Mark as archived when implementation begins

## Templates

| Template | Purpose |
|----------|--------|
| `.agents/skills/brainstorming/templates/notes.md` | Main brainstorm document - ideas, decisions, approach |
| `.agents/skills/brainstorm-to-tasks/templates/tasks.md` | Task breakdown - phases, estimates, acceptance criteria |

Customize them for your project's needs.
