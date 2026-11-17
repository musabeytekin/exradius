# Artifact storage and delivery

Artifacts are files of any type produced by an agent: reports, images, archives, patches,
spreadsheets, and binary files. Exradius stores their bytes in the existing private object
store and their metadata in Postgres. Uploading a file and submitting a result are separate
operations. Upload failures do not prevent a result without files; there is no required
deliverables list and no new workspace-retention policy.

## Agent tools

The runner's built-in MCP server exposes:

| Tool | Behavior |
| --- | --- |
| `artifact_upload(path)` | Read a regular file inside the run's working directory, upload it, and return its UUID, name, type, size, SHA-256 and selection status. |
| `artifact_list()` | List this run's stored files, including files not selected for delivery. |
| `artifact_get(artifact_id)` | Read one stored file's metadata in this run. |
| `submit_result(output, artifact_ids?)` | Replace the result and select stored files to deliver. An omitted or empty list selects no files. Stored files are not deleted. |

Paths for `artifact_upload` are absolute inside `/work` or relative to `/work`. For example,
`/work/otr-shop/report.pdf` and `otr-shop/report.pdf` name the same file. A missing-file
error includes the resolved path and the directory against which relative paths are read.
Symlinks (including directory components), devices, directories and named pipes are refused.
The runner reads through directory file descriptors without following links.

An upload succeeds only after object storage and the metadata transaction have completed.
Retrying the same name and bytes in the same run returns the same ID. Different bytes produce
a new immutable artifact. Files remain stored when a result is replaced or the workspace is
removed; only the latest result's selected files appear in `task.result`. Admins can still
download unselected files in the task panel.

## API and limits

- `POST /internal/runs/{run_id}/artifacts?name=report.pdf`: raw file bytes; returns `201`
  with stored metadata. The run token scopes all operations to that run.
- `GET /internal/runs/{run_id}/artifacts`: metadata list.
- `GET /internal/runs/{run_id}/artifacts/{id}`: metadata for a single owned file.
- `POST /internal/runs/{run_id}/result`: `{"output": ..., "artifact_ids": ["uuid"]}`.
  Another run's IDs, missing IDs and malformed IDs are refused without changing the result.

Initial limits: 50 MiB per file, 250 MiB and 100 files per run, 20 upload attempts per minute
per run. Attempts to upload invalid files count toward the rate limit. The request body is
bounded while it is received, including when no Content-Length is supplied. Rate counters
live in the database; the run row serializes quota checks, selection and finalization.
File names cannot contain path separators or control characters. Storage keys use generated
IDs rather than the supplied file name. Type metadata is a display hint, not proof of safety.

The old internal `result.artifacts` base64 format remains accepted for existing runners,
using the same persistence and quota rules. Its JSON body is capped at one 50 MiB base64
file plus 1 MiB overhead. New agents use the separate binary upload endpoint.

## Bridge and admin access

`task.result.artifacts` contains `id`, `name`, `content_type` and an integration API `url`.
`GET /api/integration/v1/artifacts/{id}/content` requires an active integration bearer token.
The file must be selected, the task's correlation must name that token's bridge, and the
project's current cached configuration must still contain that bridge. Removing the bridge
and rereading the config, or revoking its token, removes access. Tokens sharing a bridge name
have the same project scope, consistent with the existing integration directory contract.

The kit fetches by ID using its configured Exradius API address and integration token.
It never sends the token to a host from an event or follows a download redirect. Historical
artifact URLs are accepted only to extract their UUID; their host and signature are ignored.
GitHub and Jira use the kit's authenticated download, and Desk uses it after checking its
own user's project access. A bridge continues to use its existing platform delivery method;
GitHub still publishes files on its artifacts branch. That copy inherits the repository's
visibility and is independent of later Exradius access revocation.

`GET /api/artifacts/{id}/content` is now admin-session-only. Old artifact signatures are no
longer accepted. Downloads use attachment disposition, encoded file names, octet-stream,
`nosniff`, a restrictive content security policy and private/no-store caching. Exradius does
not execute uploads or extract archives. This does not certify files as malware-free.
Transcript signatures are outside this change and retain their existing behavior.

Audit logs record artifact/run IDs, upload size and the download principal, without file
contents, bearer tokens or signed URLs. Storage credentials remain in the backend; the
agent never receives them. Serve API traffic over the existing trusted cluster network or
HTTPS/Tailscale; the object store is not a public download endpoint.

## Upgrade

Migration `0011` preserves existing artifact records as selected deliveries. Their historical
sizes and checksums are unknown. API/worker, runtime bundle and bridges must be upgraded
together: old bridge binaries cannot use the authenticated artifact download route. The old
runner submission format remains supported during rollout. No changes to project YAML or
the Go API/CRDs are required.

Independent uploads are retained without automatic expiry in this version. Storage lifecycle
cleanup, malware scanning and user-created sharing links are separate features. A failure
between an object-store write and a database commit can leave an unreferenced object; retrying
that upload uses the same storage key. There is no cross-store transaction or orphan sweeper.
