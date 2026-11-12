# Ideas not yet designed

Candidates agreed as worth designing, not yet scheduled. Each becomes its own design note when
it is picked up. Source: `reports/Enterprise engineering task patterns.md` and the brainstorm
of 2026-11-08.

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

## Secret management for more than one team

A review of the secret handling on 2026-11-11 found the run side sound (no model, repository
or HTTP connection credential in a pod, late decryption in `resolve.py`, a worker that
writes Secrets it cannot read) and four gaps on the storage and governance side. Two are
closed: stores are bound to the projects allowed to use them, with an append-only change
log, and HashiCorp Vault is the first external store (`config-repo.md` §7). Still open:

- **One master key, no rotation.** `SecretBox` holds a single Fernet key, with no key version
  on the stored values and no command to re-encrypt them. The session secret and the
  download signing key are derived from the same key, so one leak opens everything. Ideas:
  `MultiFernet` with an ordered key list, an `exradius rotate-key` command that re-encrypts
  every `*_encrypted` column, separate keys for signing.
- **Further backends.** Stores can be kept in Exradius or in Vault. AWS Secrets Manager,
  Google Secret Manager and Azure Key Vault would be further kinds behind the same backend
  protocol (`exradius/secret_backends`), read with the pod's cloud identity as `storage.py`
  does. The master key itself is still a plain Kubernetes Secret; it could be wrapped by a
  KMS key.
- **One admin, no roles.** There is a single admin user, who owns every store. Ideas: admin
  users with roles (who may change which stores), or SSO, as its own topic.

Smaller, seen in the same review: the egress proxy fetches decrypted credentials from the
api over plain HTTP inside the cluster (NetworkPolicy, no mTLS), and the Terraform modules
set no customer-managed KMS key for Kubernetes Secrets (`kubernetes.md` §10 leaves it as
install guidance).
