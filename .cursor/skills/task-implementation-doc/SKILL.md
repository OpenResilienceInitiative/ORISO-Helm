---
name: task-implementation-doc
description: Create a compact implementation plan with subtasks, status, and verification checklist that drives the goal-loop iterations. Use after the spike is done and before implementation starts.
---

# Task Implementation Document

Create `02-implementation-plan.md` in the current task folder.

Structure:

- **Objective**: one line
- **Impacted files**: paths only
- **Subtasks**: table with columns `#`, `Subtask`, `Files`, `Verify with`, `Status`
    - Each subtask must be small enough for one loop iteration and have a concrete verify command using this repo’s toolchain (`helm lint`, `helm template` when chart/values/templates change; otherwise "not applicable")
    - Status values: `todo`, `doing`, `done`, `blocked`
- **Verification checklist**: `helm lint` and `helm template` when Helm paths are touched; otherwise "not applicable"
- **Risks**

Update the Status column as the loop progresses — this file is the loop's source of truth for what's next.
