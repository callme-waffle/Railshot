# Branch and Work Governance

These rules supplement the repository guidance in [AGENT.md](AGENT.md). Follow both files. The repository layout and ownership rules in `AGENT.md` remain in force.

## Branch Names

Use `TAG/BRIEF` for task branches:

- `feat`: new functionality
- `refactor`: restructuring or internal change
- `docs`: documentation
- `design`: design work
- `chore`: maintenance
- `fix`: defect correction
- `test`: test work

Keep `BRIEF` concise and descriptive. The current development aggregation branch `dev/v1.0.0` is an explicit exception to this naming convention; do not rename it under these rules.

## Task Workflow

For each work request, proceed in this order:

1. Turn the request into a concrete work specification, including scope and completion criteria.
2. Make a plan based on that specification.
3. Carry out the work: implement or edit code, run relevant tests, write the pull request description, and create and publish the pull request.

During implementation, actively delegate code writing and edits to sub-agents, assigning them clear, bounded tasks. The main agent handles coordination, task assignment, review, integration, integration testing, and verification of results; code implementation is the sub-agents' responsibility. Select models according to task difficulty and use token budgets efficiently. If authentication or permissions prevent creating or publishing the pull request, report that exact step as incomplete and state the blocking reason; do not claim the work is complete.

Use an independent worktree for each task and commit the completed changes on its task branch. Do not include unrelated changes in the task commit.

## Repository Preservation

Continue to follow `AGENT.md`'s repository structure and ownership rules. Do not make arbitrary structural changes. The user-approved legacy archive may preserve the code from remote `main` at `645e28a`; keep the currently verified on-premises path in place. These specific approved activities do not authorize other restructuring or moving code.

## Pull Request Description

Use `.github/pull_request_template.md` and retain its section order and field structure. Describe endpoints as `해당없음` when a change has none. Distinguish tests that were not run from results that were not verified; never report either as a pass.
