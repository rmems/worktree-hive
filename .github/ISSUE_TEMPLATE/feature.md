---
name: Feature request or new functionality
about: Suggest a feature for worktrees-hives (Rust/PYTHON/SUBAGENT skill)

labels: ["platform","size:S"]
assignees: []
body:
- type: markdown
  attributes:
    value: |
      ## 🧠 Feature request — spawn to PRs & babysit

      This repo implements a Python/Rust hybrid where AI agents convert GitHub issues → isolated git worktrees (never auto merge). Please keep descriptions concise for subagent spawning clarity.

- type: input
  id: feature-title
  attributes:
    label: Feature name or short title
    description: One-line summary of the new capability needed
    placeholder: e.g., "Add stack-aware ordering before PR creation"

- type: textarea
  id: why-needed
  attributes:
    label: Why is this feature necessary? What does it solve for agents/worktrees?
    description: |
      Explain use cases, blockers without this change, and how you expect it to improve reliability.
    placeholder: "Our current implementation requires linear PR order when parent failures block all children..."

- type: textarea
  id: specs-or-pr-ref
  attributes:
    label: Specifics / design notes (optional) — or linked GitHub/Linear issue if exists?
    description: |
      Provide any constraints, expected behavior from subagent perspective. Link to Linear #RM-* project worktrees-hives if applicable.

- type: textarea
  id: test-plan-or-repro-steps
  attributes:
    label: Test plan or repro steps (for features that modify behavior) — optional for docs-only changes?
    description: |
      How would you verify this works once implemented, especially from subagent spawning angle?

- type: checkboxes
  id: accept-safety-rules
  attributes:
    label: You understand the non-negotiable rules (never merge PRs automatically; force-with-lease only) ✅
    options:
      - label: I confirm this feature does not auto-trigger merges or bypass safety gates
