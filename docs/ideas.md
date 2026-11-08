# Ideas not yet designed

Candidates agreed as worth designing, not yet scheduled. Each becomes its own design note when
it is picked up. Source: `reports/Enterprise engineering task patterns.md` and the brainstorm
of 2026-11-08.

## Exradius's own egress proxy

One proxy that Exradius runs on the runs network, which every run, setup and service container
uses through `HTTP(S)_PROXY`, with `NO_PROXY` covering `exradius-api` and the run's services.

- Setup phase (image builds, setup commands, services): open; forwards to the corporate proxy
  when the install names one.
- Agent phase: an allowlist only, built from what the run already declares: the model
  endpoint, the repository hosts, the mirrors, the declared connections. Decided on the
  `CONNECT` host name, so nothing is decrypted.
- Enforced at network level (a container that ignores the proxy variables reaches nothing),
  per the run-environment principle (docs/run-environment.md).
- Every host a run reached, or was refused, goes into the run's record.

It answers two report items at once: corporate networks (proxy, CA, mirrors at install level)
and no path to production from the agent phase.

## Harness-run checks

`exradius.yaml` declares check commands (tests, lint, build). After the agent submits its
result, the worker runs them in a fresh workstation container from the pushed head commit
and records the verbatim outcome in the result, next to what the agent claimed.

- For a bug: the tests the agent added are also run on the base commit, so the result shows
  "fails before, passes after" as harness evidence, not the agent's word.
- Keeps "the worker does not act on behalf of the agent": it checks, it changes nothing.
- Separates "failed" from "could not run" (missing tool, service down) from "flaky" (passes
  on rerun).

## Evaluating Exradius on real tasks

A system-level eval: not the model alone (SWE-bench does that) but everything a task goes
through in Exradius: config repo, workstation and services, operating rules and prompt,
agent, questions and answers, delivery. To be planned in detail as its own topic, together
with the loop of improvements it drives. Settled so far:

- **Question it answers:** can Exradius take a real ticket, resolve it and deliver it? So far
  every ticket was written by us, small and clear, and none was graded against a known fix.
- **Dataset:** real upstream bugs from the configured projects (Plane, Saleor, Outline; not
  Bank of Anthos, a demo with few real bugs). Each fixed by an upstream PR merged after the
  model's training cutoff. Mixed: backend logic, API and permissions, frontend UI, one
  cross-repository (Saleor and its dashboard), one vague report. About 12 to start.
- **Base:** per project, a trial branch on the fork at an upstream commit that predates every
  fix in the set, so one config branch serves all its bugs.
- **Ticket:** the original issue text as reported, without links to the fix. The user answers
  questions as the reporter.
- **Graders:** the upstream fix's tests run on the agent's branch, which the agent never saw;
  the project's existing tests still pass; where upstream added no test, a review of the diff
  against the upstream fix (correct / partial / wrong). An LLM judge only after it agrees with
  the user's judgment.
- **Metrics:** resolve rate, mergeable as is, reproduced before fixing, time, cost, questions.
- **Error analysis:** every failure attributed to harness, environment, rules or prompt, or
  model. With about 12 tasks the cause list matters more than the percentage.
- **Repeats:** each task 2 or 3 times; agents vary between runs.
- **Reusable suite:** dataset, runner (publishes tasks via the CLI) and grader kept so the
  same set is rerun after every change to rules, prompt, harness or model. The hidden-test
  grader overlaps with harness-run checks above.
- **Expected to expose:** UI bugs (no browser in the workstation), no base ref in the task
  envelope, no per-run usage or cost recorded.

## MCP tool lists that change on their own

Agent profiles for routing (`agent-routing.md` §3) carry each connection's tools from
`tools/list`, stored by the last Verify. A connection's tools can change with no commit on our side: GitHub's MCP is a remote
server (`api.githubcopilot.com/mcp/`), and the catalog's `npx -y` / `uvx` packages pull the
latest version. The stored list then goes stale silently. Noted on 2026-11-08, to look at
later. Ideas so far: show when the tools were fetched, refetch on every Verify, pin catalog
package versions so a change needs a commit.
