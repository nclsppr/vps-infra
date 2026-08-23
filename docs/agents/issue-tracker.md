# Issue tracker: GitHub

Issues and specifications for this repository live in GitHub Issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for a multiline body.
- **Read an issue**: `gh issue view <number> --comments`. Fetch labels and use `jq` when the output needs filtering.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`. Add `--label` and `--state` filters as needed.
- **Comment on an issue**: `gh issue comment <number> --body "..."`.
- **Apply or remove labels**: `gh issue edit <number> --add-label "..."` or `gh issue edit <number> --remove-label "..."`.
- **Close an issue**: `gh issue close <number> --comment "..."`.

Infer the repository from `git remote -v`. The `gh` CLI does this when it runs inside the checkout.

## Pull requests as a triage surface

**PRs as a request surface: no.** Set this value to `yes` if this repository later treats external pull requests as feature requests.

When the value is `yes`, process pull requests with the same labels and states as issues:

- **Read a pull request**: `gh pr view <number> --comments` and `gh pr diff <number>`.
- **List external pull requests for triage**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`. Keep `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, and `NONE`. Drop `OWNER`, `MEMBER`, and `COLLABORATOR`.
- **Comment, label, or close**: use `gh pr comment`, `gh pr edit --add-label` or `--remove-label`, and `gh pr close`.

GitHub uses one number sequence for issues and pull requests. Resolve a bare `#42` with `gh pr view 42`, then fall back to `gh issue view 42`.

## Skill operations

- When a skill says "publish to the issue tracker", create a GitHub issue.
- When a skill says "fetch the relevant ticket", run `gh issue view <number> --comments`.

## Wayfinding operations

The `/wayfinder` skill uses one map issue and a set of child issues.

- **Map**: create one issue with the `wayfinder:map` label. Store Notes, Decisions-so-far, and Fog in its body.
- **Child ticket**: link the issue to the map as a GitHub sub-issue. If sub-issues are unavailable, add the child to a task list in the map and put `Part of #<map>` at the top of the child. Use a `wayfinder:<type>` label with `research`, `prototype`, `grilling`, or `task`. Assign the ticket to the developer who claims it.
- **Blocking**: use GitHub issue dependencies. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`. Get the blocker database ID with `gh api repos/<owner>/<repo>/issues/<number> --jq .id`. If dependencies are unavailable, add `Blocked by: #<number>` at the top of the child.
- **Frontier query**: list the map's open children. Drop assigned children and children with an open blocker. Select the first remaining child in map order.
- **Claim**: run `gh issue edit <number> --add-assignee @me`. This is the session's first write.
- **Resolve**: comment with the answer, close the child, then add a context pointer with its link to the map's Decisions-so-far section.
