# PullSage REST API

## Overview

The FastAPI entry point is:

```text
uv run pullsage-api
```

Unless configured otherwise, the base URL is `http://127.0.0.1:8000`. JSON is the only application payload format. CORS is disabled because the MVP is a service/webhook API rather than a browser-facing application.

The API has two route groups:

- operational and webhook routes at the root;
- manual review and capability routes under `/api/v1`.

The API does not expose GitHub credentials, webhook secrets, Codex prompts, complete private diffs, or subprocess arguments. Job data is held only in memory and is lost on restart.

## Conventions

### Content type

Send:

```http
Content-Type: application/json
```

Webhook signatures are calculated over the exact raw bytes sent in the request, not a re-serialized JSON document.

### Request correlation

PullSage assigns a request ID and includes it in responses/log context. If a client supplies a valid request ID header, the application may preserve it; otherwise it generates one. Record the returned request ID when diagnosing a failure.

### Time and identifiers

- Job IDs are UUID strings.
- Timestamps are timezone-aware ISO 8601 UTC strings.
- Pull-request numbers are positive integers.
- Job statuses are `queued`, `fetching_context`, `reviewing`, `validating`, `posting`, `completed`, or `failed`.

### Asynchronous jobs

`POST /api/v1/reviews` and accepted webhooks return before GitHub context fetching or Codex review completes. Poll the returned job ID. A terminal job remains available only until `PULLSAGE_JOB_RETENTION_SECONDS` expires.

### Error envelope

Expected failures use a safe JSON envelope:

```json
{
  "error": {
    "code": "github_not_found",
    "message": "The requested GitHub resource was not found.",
    "request_id": "9b5703b1-14ad-4af6-b5bd-dca77a675503"
  }
}
```

Validation errors use the same top-level `error` shape and may include safe field-level details. Unexpected failures return a generic message; raw tracebacks stay in redacted application logs.

Common codes include:

| Code | Meaning |
| --- | --- |
| `validation_error` | Request fields failed validation |
| `invalid_webhook_signature` | Signature is missing, malformed, or incorrect |
| `missing_github_event` | `X-GitHub-Event` is absent |
| `missing_github_delivery` | `X-GitHub-Delivery` is absent |
| `invalid_webhook_payload` | Verified webhook JSON is malformed or incomplete |
| `configuration_error` | A required runtime setting is unavailable |
| `github_authentication_error` | GitHub rejected the configured credential |
| `github_rate_limit_error` | GitHub rate limit prevents the request |
| `github_not_found` | Repository or pull request was not found |
| `github_api_error` | GitHub could not complete the request |
| `pull_request_too_large` | File-count or diff limit was exceeded |
| `too_many_changed_files` | Changed-file count exceeded its configured limit |
| `codex_not_found` | The configured Codex executable was not found |
| `codex_timeout` | Codex exceeded its configured timeout |
| `codex_runtime_error` | Codex failed to start, authenticate, or complete |
| `invalid_codex_output` | Model output failed validation after the repair attempt |
| `review_validation_error` | A review failed domain validation |
| `review_posting_error` | GitHub did not accept the review submission |
| `job_not_found` | Job ID is unknown, expired, or lost on restart |
| `queue_unavailable` | Worker queue is starting or shutting down |
| `internal_error` | Unexpected server failure |

## Endpoint summary

| Method | Path | Success | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health` | `200` | Liveness only |
| `GET` | `/ready` | `200` or `503` | Dependency and worker readiness |
| `GET` | `/api/v1/config/capabilities` | `200` | Safe capability switches and limits |
| `POST` | `/api/v1/reviews` | `202` | Enqueue a manual review |
| `GET` | `/api/v1/reviews/{job_id}` | `200` | Read an in-memory review job |
| `POST` | `/webhooks/github` | `202` | Verify and accept/ignore a GitHub delivery |

## `GET /health`

Basic process liveness. This endpoint deliberately does not call GitHub or Codex and does not reveal environment or credential state.

### Response

`200 OK`

```json
{
  "status": "healthy",
  "service": "pullsage"
}
```

### Example

```bash
curl --fail-with-body http://127.0.0.1:8000/health
```

PowerShell:

```powershell
curl.exe --fail-with-body http://127.0.0.1:8000/health
```

## `GET /ready`

Reports whether the process is ready to accept useful work. Checks are informative and independent: missing external setup produces a degraded payload rather than a startup crash.

### Checks

- settings loaded;
- queue worker running;
- GitHub token configured;
- webhook secret configured;
- Codex executable available.

### Ready response

`200 OK`

```json
{
  "status": "ready",
  "checks": {
    "settings_loaded": true,
    "worker_running": true,
    "github_token_configured": true,
    "webhook_secret_configured": true,
    "codex_available": true
  }
}
```

### Degraded response

`503 Service Unavailable`

```json
{
  "status": "degraded",
  "checks": {
    "settings_loaded": true,
    "worker_running": true,
    "github_token_configured": false,
    "webhook_secret_configured": false,
    "codex_available": false
  }
}
```

No value explains *what* the token or secret is. Readiness is safe for operational probing, but production ingress should still limit its exposure.

## `GET /api/v1/config/capabilities`

Returns non-secret, effective capability information. It is useful before requesting work, but it is not an authorization grant.

### Response

`200 OK`

```json
{
  "posting_enabled": false,
  "codex_available": true,
  "mcp_write_tools_enabled": false,
  "max_diff_chars": 200000,
  "max_changed_files": 100,
  "max_concurrent_reviews": 2,
  "in_memory_jobs": true
}
```

The response never contains `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET`, `CODEX_MODEL`, private diff data, or raw command arguments.

### Example

```bash
curl --fail-with-body http://127.0.0.1:8000/api/v1/config/capabilities
```

## `POST /api/v1/reviews`

Validates and enqueues a manual pull-request review.

### Request body

```json
{
  "owner": "octo-org",
  "repository": "example",
  "pull_request_number": 42,
  "post_comments": false
}
```

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `owner` | string | yes | Non-empty GitHub repository owner |
| `repository` | string | yes | Non-empty GitHub repository name, without owner |
| `pull_request_number` | integer | yes | Greater than zero |
| `post_comments` | boolean | no | Defaults to `false` |

`post_comments=true` is the manual endpoint's explicit write authorization. It can post even when automated webhook posting is disabled, so keep the default false and do not expose this endpoint to untrusted callers. `PULLSAGE_POST_COMMENTS` controls automated webhook jobs; it is not required for an explicitly authorized manual request.

### Accepted response

`202 Accepted`

```json
{
  "job_id": "a53a60ae-865a-4b77-851f-63cd7e48c7a8",
  "status": "queued",
  "deduplicated": false,
  "message": "Review job accepted"
}
```

When equivalent work is already active, the endpoint returns `202` with the active job ID, `deduplicated: true`, and `message: "An equivalent review job is already active"`.

### Other responses

| Status | Condition |
| --- | --- |
| `202` | New job accepted |
| `202` | Equivalent active work deduplicated to an existing job |
| `422` | Request validation failed |
| `503` | Queue is unavailable during startup or shutdown |

GitHub and Codex failures normally occur after admission and appear as a terminal `failed` job rather than changing the original `202`.

### cURL example

```bash
curl --fail-with-body \
  --request POST \
  --header "Content-Type: application/json" \
  --data '{"owner":"octo-org","repository":"example","pull_request_number":42,"post_comments":false}' \
  http://127.0.0.1:8000/api/v1/reviews
```

PowerShell:

```powershell
$request = @{
  owner = "octo-org"
  repository = "example"
  pull_request_number = 42
  post_comments = $false
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/reviews `
  -ContentType application/json `
  -Body $request
```

## `GET /api/v1/reviews/{job_id}`

Returns one job from the current API process.

### Path parameter

| Parameter | Type | Rules |
| --- | --- | --- |
| `job_id` | UUID | ID returned by an enqueue response |

### In-progress response

`200 OK`

```json
{
  "job_id": "a53a60ae-865a-4b77-851f-63cd7e48c7a8",
  "owner": "octo-org",
  "repository": "example",
  "pull_request_number": 42,
  "source": "manual",
  "post_comments": false,
  "head_sha": null,
  "status": "reviewing",
  "created_at": "2026-07-23T10:15:00Z",
  "started_at": "2026-07-23T10:15:01Z",
  "completed_at": null,
  "error": null,
  "result": null
}
```

### Completed response

`200 OK`

```json
{
  "job_id": "a53a60ae-865a-4b77-851f-63cd7e48c7a8",
  "owner": "octo-org",
  "repository": "example",
  "pull_request_number": 42,
  "source": "manual",
  "post_comments": false,
  "head_sha": null,
  "status": "completed",
  "created_at": "2026-07-23T10:15:00Z",
  "started_at": "2026-07-23T10:15:01Z",
  "completed_at": "2026-07-23T10:15:18Z",
  "error": null,
  "result": {
    "summary": "No high-confidence defects were identified in the supplied changes.",
    "verdict": "comment",
    "confidence": 0.88,
    "risk_level": "low",
    "findings": [],
    "testing_recommendations": [
      "Run the repository test suite in CI; PullSage did not execute it."
    ],
    "limitations": [
      "Review was limited to the supplied pull-request context."
    ]
  }
}
```

### Failed response

The job resource still returns `200 OK`; failure is part of the asynchronous job state:

```json
{
  "job_id": "a53a60ae-865a-4b77-851f-63cd7e48c7a8",
  "owner": "octo-org",
  "repository": "example",
  "pull_request_number": 42,
  "source": "manual",
  "post_comments": false,
  "head_sha": null,
  "status": "failed",
  "created_at": "2026-07-23T10:15:00Z",
  "started_at": "2026-07-23T10:15:01Z",
  "completed_at": "2026-07-23T10:15:02Z",
  "error": "The configured Codex executable is unavailable.",
  "result": null
}
```

### Other responses

| Status | Condition |
| --- | --- |
| `404` | Unknown, expired, or restart-lost job ID |
| `422` | Path value is not a valid UUID |

Do not assume `404` proves that a review was never submitted to GitHub. The job store is intentionally ephemeral.

### Example

```bash
curl --fail-with-body \
  http://127.0.0.1:8000/api/v1/reviews/a53a60ae-865a-4b77-851f-63cd7e48c7a8
```

## `POST /webhooks/github`

Receives GitHub webhook deliveries. This route is for GitHub, not general manual API clients.

### Required headers

| Header | Purpose |
| --- | --- |
| `Content-Type: application/json` | Payload encoding |
| `X-Hub-Signature-256` | `sha256=` plus HMAC SHA-256 of the exact body |
| `X-GitHub-Event` | Must be `pull_request` for review admission |
| `X-GitHub-Delivery` | Unique GitHub delivery ID used for bounded deduplication |

PullSage verifies the raw body before JSON parsing. It does not reveal whether a failure was due to length, encoding, or digest mismatch.

### Supported payload subset

After verification, PullSage extracts only the fields required to identify the repository, pull request, action, draft state, and head SHA. It supports:

- `opened`
- `reopened`
- `synchronize`
- `ready_for_review`

Unsupported events/actions are ignored. Draft PRs are ignored unless the action is `ready_for_review`.

### Accepted response

`202 Accepted`

```json
{
  "status": "accepted",
  "reason": "Review job accepted",
  "job_id": "7c783bcb-726f-4b92-9518-003c108fd6aa"
}
```

### Ignored response

Ignored deliveries are acknowledged without work so GitHub does not needlessly retry:

`202 Accepted`

```json
{
  "status": "ignored",
  "reason": "Unsupported pull-request action"
}
```

Duplicate deliveries use `status: "duplicate"` with `reason: "GitHub delivery was already processed"`. An equivalent active head also uses `duplicate`, returns its job ID, and explains that the head is already queued. Other ignored reasons are `Unsupported GitHub event` and `Draft pull requests are ignored until ready for review`.

### Response codes

| Status | Condition |
| --- | --- |
| `202` | Accepted or safely ignored verified delivery |
| `400` | Verified body is malformed or lacks required pull-request identifiers |
| `401` | Signature header is missing or the signature is invalid |
| `413` | Request exceeds an ingress/body limit, when configured |
| `422` | Verified payload fields fail validation |
| `503` | Webhook secret is unconfigured or worker queue is unavailable |

### cURL shape

Generate the signature from the exact bytes in `payload.json`; do not paste a real webhook secret into shell history:

```bash
curl --request POST \
  --header "Content-Type: application/json" \
  --header "X-GitHub-Event: pull_request" \
  --header "X-GitHub-Delivery: local-test-delivery-001" \
  --header "X-Hub-Signature-256: sha256=<computed-hmac>" \
  --data-binary @payload.json \
  http://127.0.0.1:8000/webhooks/github
```

See [GitHub setup](github-setup.md) for a PowerShell example that signs and sends identical bytes.

## Structured review object

The `result` field and MCP review tools use the strict review shape below:

| Field | Type | Notes |
| --- | --- | --- |
| `summary` | string | Overall, evidence-based explanation |
| `verdict` | enum | `approve`, `comment`, `request_changes` |
| `confidence` | number | Inclusive range `0.0` to `1.0` |
| `risk_level` | enum | `low`, `medium`, `high`, `critical` |
| `findings` | array | Validated findings, possibly empty |
| `testing_recommendations` | string array | Specific tests; never claims they ran |
| `limitations` | string array | Coverage/context caveats |

Each finding has:

```json
{
  "id": "stable-finding-id",
  "title": "Short actionable title",
  "body": "Concise developer-focused explanation.",
  "severity": "high",
  "category": "reliability",
  "confidence": 0.93,
  "file_path": "src/example.py",
  "line": 28,
  "start_line": 28,
  "side": "RIGHT",
  "suggested_fix": "Handle the failure before returning.",
  "evidence": "The new branch leaves the resource open when the await fails."
}
```

Controlled finding values:

- severity: `info`, `low`, `medium`, `high`, `critical`;
- category: `correctness`, `security`, `reliability`, `performance`, `maintainability`, `testing`;
- side: `RIGHT`.

Optional `line`, `start_line`, and `suggested_fix` fields may be `null` when the evidence cannot be mapped safely. `file_path` and evidence remain required. Posting validation is stricter than parsing: low-confidence, duplicate, unchanged-path, or unsafe-line findings do not become inline comments.

## Security and deployment notes

- Keep the default host `127.0.0.1` for local development.
- Put a production webhook behind TLS and a hardened reverse proxy.
- Add authentication/authorization to manual review and job routes before exposing them to untrusted clients.
- Preserve raw request bytes between ingress and PullSage.
- Configure ingress request limits as well as PullSage's diff/file limits.
- Never use query parameters for tokens or webhook secrets.
- Rate-limit public endpoints in production.
- A readiness response is diagnostic, not proof that a token is authorized for a particular repository.
