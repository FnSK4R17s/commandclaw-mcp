---
name: brainstorm-to-tasks
description: Generate a structured tasks.md file from a brainstorming document. Use when a brainstorm has matured and is ready for implementation planning.
license: MIT
compatibility: Requires shell and view_file capabilities
metadata:
  author: FnSK4R17s
  version: "1.1"
---

# Brainstorm to Tasks

Generate a structured `tasks.md` file from a brainstorming document. This skill extracts implementation phases, tasks, acceptance criteria, and dependencies from free-form brainstorming notes.

## When to Use

- Brainstorm status is "Ready for Implementation" or similar
- The notes contain enough detail to define concrete tasks
- You want to break down the work into phases with estimates

## Prerequisites

1. A brainstorming document exists (e.g., `brainstorming/issue-042-*/notes.md`)
2. The document contains implementation details, phases, or a technical approach section

## Execution Steps

### 1. Read the Brainstorming Document

```bash
cat brainstorming/issue-${ISSUE_NUM}-*/notes.md
```

### 2. Read the Guiding Documents

```bash
cat guiding_docs/VISION.md
```

Key things to verify against the vision:
- Does the feature align with the three security layers (phantom tokens, RBAC, sessions)?
- Does it use the correct tech stack (FastAPI, FastMCP, Cerbos, Redis)?
- Is it in scope for the current phase?

### 3. Extract Key Sections

Identify these sections in the brainstorming document:

| Section to Find | Maps To |
|-----------------|---------|
| Technical Approach / Architecture | Task breakdown structure |
| Implementation Phases / Steps | Phase definitions |
| API Endpoints | Scope definition |
| Open Questions (resolved) | Implementation decisions |
| Module / Class Design | Component task list |
| Security Considerations | Acceptance criteria |

### 4. Research the Codebase

Before defining tasks, check existing code and imports:

```bash
# Find where a module is imported
grep -rn "from commandclaw_mcp" src/ --include="*.py"

# Check existing patterns
grep -rn "similar_function" src/ --include="*.py" | head -10

# Find test files for the module
find tests/ -name "test_*.py" | grep module_name
```

**What to extract:**
1. **Direct importers** -- files that import the module you're modifying
2. **Existing patterns** -- how similar features are already implemented
3. **Test coverage** -- existing tests that exercise related code
4. **Config dependencies** -- env vars or config keys the feature needs

### 5. Define Phases

Common phase patterns for this project:

| Pattern | Phases |
|---------|--------|
| **New Module** | 1. Scaffolding -> 2. Core Types (Pydantic) -> 3. Implementation -> 4. Middleware Integration -> 5. Testing |
| **New Feature** | 1. Research -> 2. API Design -> 3. Implementation -> 4. Integration -> 5. Testing |
| **Security Feature** | 1. Threat Model -> 2. Core Crypto -> 3. Middleware -> 4. Policy Config -> 5. Audit + Testing |
| **Refactor** | 1. Analysis -> 2. Extract -> 3. Migrate -> 4. Cleanup -> 5. Verify |

### 6. Extract Tasks from Content

For each phase, identify discrete tasks by looking for:

- **Action items**: "Create", "Add", "Implement", "Migrate", "Update"
- **Module/class lists**: Each module becomes a task
- **API endpoints**: Each endpoint group becomes a task
- **File changes**: Major file changes become tasks
- **Dependencies**: "After X, do Y" suggests task ordering

### 7. Estimate Tasks

| Complexity | Estimate | Examples |
|------------|----------|----------|
| Trivial | 15m | Config change, rename |
| Simple | 30m-1h | Single file, clear pattern |
| Medium | 2-4h | Multiple files, some research |
| Complex | 4-8h | New patterns, integration work |
| Large | 1-2d | Major feature, many touchpoints |

### 8. Define Acceptance Criteria

For each task, extract acceptance criteria from:

- Security requirements mentioned
- API contracts defined
- Integration points listed
- Test cases implied

**Format:**
```markdown
**Acceptance Criteria**:
- [ ] Criterion 1 (testable condition)
- [ ] Criterion 2 (measurable outcome)
- [ ] Tests pass: `pytest tests/test_<module>.py`
```

### 9. Generate tasks.md

Create the tasks file in the brainstorming folder.

## Task Template

Use this structure for each task:

```markdown
### Task X.Y: {{Task Title}}
**Priority**: P0 | P1 | P2
**Estimate**: Xh
**Files**: `src/commandclaw_mcp/module/file.py`

Brief description of what this task accomplishes.

**Acceptance Criteria**:
- [ ] Criterion 1
- [ ] Criterion 2
- [ ] Tests pass: `pytest tests/test_<module>.py -v`

---
```

## Priority Definitions

| Priority | Meaning | When to Use |
|----------|---------|-------------|
| P0 | Blocker | Other tasks depend on this |
| P1 | Critical | Core functionality |
| P2 | Important | Polish, optimization, testing |

## Output Location

Tasks file goes in the same folder as the brainstorm:

```
brainstorming/
+- issue-042-session-pooling/
    |- notes.md      # Original brainstorm
    +- tasks.md      # Generated tasks (THIS OUTPUT)
```

## Validation Checklist

After generating tasks.md, verify:

- [ ] All phases from brainstorm are represented
- [ ] Each task has clear acceptance criteria
- [ ] Estimates are realistic (not too optimistic)
- [ ] Dependencies form a valid DAG (no cycles)
- [ ] P0 tasks are truly blocking
- [ ] File paths are accurate (src/commandclaw_mcp/...)
- [ ] Total estimate aligns with scope
- [ ] Tasks align with VISION.md architecture

## Related Skills

| Skill | Relationship |
|-------|--------------|
| `brainstorming` | Source of input for this skill |
| `implement-tasks` | Next step -- executes the generated tasks |
