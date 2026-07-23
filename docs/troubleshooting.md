# Troubleshooting PullSage

## Start with safe diagnostics

Run the API and inspect liveness, readiness, and non-secret capabilities:

```powershell
uv run pullsage-api
```

In another PowerShell window:

```powershell
curl.exe -i http://127.0.0.1:8000/health
curl.exe -i http://127.0.0.1:8000/ready
curl.exe -i http://127.0.0.1:8000/api/v1/config/capabilities
```

Expected distinctions:

- `/health` returns `200` with `status: "healthy"` when the process is alive.
- `/ready` returns `200` when ready or `503` with a `degraded` checks object.
- `/api/v1/config/capabilities` shows safe effective switches/limits, never secret values.

Capture:

- the HTTP status;
- the response's `X-Request-ID`;
- the job ID, if one exists;
- the safe exception category;
- the PullSage/Codex versions and OS;
- whether the issue occurs in API, webhook, or MCP mode.

Never collect or share a token, webhook secret, authorization header, private diff, complete prompt, `.env`, or credential file.

## `codex` command not found

### Symptoms

- `/ready` reports `codex_available: false`;
- a job fails with a `codex_not_found` message;
- an MCP review says the configured Codex executable is unavailable.

### Checks

PowerShell:

```powershell
where.exe codex
Get-Command codex -ErrorAction SilentlyContinue
```

macOS/Linux:

```bash
command -v codex
```

### Fix

Install the Codex CLI through your approved process outside PullSage, then authenticate it. PullSage will not install it.

If Codex exists but PullSage cannot find it, set `CODEX_COMMAND` to the absolute executable path:

```dotenv
CODEX_COMMAND=C:/absolute/path/to/codex.exe
```

Restart PullSage or the MCP host after changing the environment. `CODEX_COMMAND` must be one executable path/name, not a compound shell command with arguments.

For Codex Desktop, remember that a GUI process may not inherit the PATH from a terminal opened later. Configure an absolute path or launch the application from an environment where Codex and `uv` are visible.

## Codex is not authenticated

### Symptoms

- Codex starts but exits nonzero;
- the job records a safe Codex runtime/authentication failure;
- direct review tools fail quickly while `codex_available` remains true.

### Checks

Run a benign Codex CLI command using the same OS account and environment that runs PullSage. Follow the installed CLI's authentication/status guidance. Do not diagnose by printing authentication files, tokens, cookies, or environment variables.

### Fix

Authenticate Codex interactively outside PullSage, then retry a dry-run review. Service accounts, containers, Windows services, and desktop apps may use a different home/profile from your terminal; authentication must be available to the actual process identity.

PullSage never attempts login and does not accept an OpenAI API key as a replacement for a functioning requested Codex CLI runtime.

## Codex times out

### Symptoms

- job ends `failed` with a timeout category;
- MCP tool exceeds its host tool timeout;
- temporary high CPU/latency appears during review.

### Checks

- Compare `CODEX_TIMEOUT_SECONDS` with the MCP host `tool_timeout_sec`.
- Check whether the diff is near `PULLSAGE_MAX_DIFF_CHARS`.
- Confirm local Codex works on a small benign prompt.
- Check local resource pressure without dumping the PR context.

### Fix

- Keep the review bounded; lower the diff/file limits for constrained machines.
- Increase `CODEX_TIMEOUT_SECONDS` only after assessing cost and availability impact.
- Set the MCP tool timeout high enough for one normal attempt plus the single possible repair attempt and GitHub latency.
- Reduce concurrent reviews with `PULLSAGE_MAX_CONCURRENT_REVIEWS`.

PullSage intentionally does not retry indefinitely.

## Malformed or invalid model output

### Symptoms

- job fails with `invalid_codex_output`;
- logs mention JSON/schema/domain validation;
- no GitHub review is posted.

### Behaviour

PullSage attempts one constrained repair using validation errors. If the second output is invalid, it fails rather than posting untrusted text.

### Checks

- Confirm the installed Codex version supports `--output-schema` and `--output-last-message`.
- Confirm a configured `CODEX_MODEL` supports reliable structured output.
- Review safe validation field names/categories in logs; do not log the complete private output.
- Check whether filenames/line data from GitHub were incomplete.

### Fix

- Update the configured model choice only through normal operator configuration.
- Correct a schema/runner incompatibility in code and add an offline regression test.
- Retry only after identifying a transient cause.
- Never bypass Pydantic/domain validation or paste raw stdout directly into GitHub.

## Invalid GitHub token

### Symptoms

- GitHub operations fail with authentication;
- `/ready` says the token is configured, but calls still receive `401` or `403`;
- metadata reads work for one repository but not another.

`github_token_configured: true` means only that a non-empty value exists, not that GitHub accepts it.

### Checks

- Confirm the token is unexpired and not revoked.
- Confirm the PullSage process received the intended secret injection.
- Confirm selected-repository access.
- Confirm organization approval/SSO authorization.
- Confirm pull-request read permission; posting also needs write permission.
- If loading from a file, remove an unintended trailing newline.

### Fix

Create/rotate a least-privilege fine-grained token and restart the process. Do not print or place the token in a URL to test it. See [GitHub setup](github-setup.md).

## GitHub returns `404`

Check:

- owner and repository spelling;
- PR number;
- whether the token is limited to other repositories;
- organization approval;
- `GITHUB_API_URL` for GitHub.com versus GitHub Enterprise.

GitHub may return `404` for a private repository the credential cannot access.

## GitHub rate limit

### Symptoms

- job fails with a GitHub rate-limit category;
- safe metadata may include a reset/retry time;
- many concurrent or repeated reviews fail together.

### Fix

- Wait until the reported safe reset/retry time.
- Reduce concurrent reviews.
- Investigate duplicate/manual review traffic.
- Avoid repeatedly polling GitHub through MCP tools when one fetched result is enough.
- Move production use to installation-scoped GitHub App tokens and enforce quotas.

Do not make an unbounded automatic retry loop.

## Webhook signature failure

### Symptoms

- `POST /webhooks/github` returns `401`;
- GitHub Recent deliveries shows an authorization failure;
- the request never creates a job.

### Checks

1. Confirm the webhook and PullSage use the same `GITHUB_WEBHOOK_SECRET`.
2. Confirm the secret is not the GitHub token.
3. Confirm `X-Hub-Signature-256` reaches the app.
4. Confirm the value begins with `sha256=`.
5. Confirm the proxy does not parse/reformat/decompress/replace the body before FastAPI reads it.
6. Confirm the signature is over the exact bytes sent.
7. Restart PullSage after changing its environment.

### Fix

Update both sides in a coordinated change. Use GitHub's redelivery after the service has the correct secret. Do not weaken verification, compare plain strings, accept SHA-1, or parse JSON before checking the digest.

An invalid-signature response intentionally does not reveal which comparison detail failed.

## No webhook deliveries arrive

Check GitHub's webhook settings and **Recent deliveries**:

- webhook is active;
- payload URL ends with `/webhooks/github`;
- public DNS resolves;
- TLS certificate is valid;
- SSL verification is enabled;
- the event subscription includes **Pull requests**;
- the action is one of `opened`, `reopened`, `synchronize`, `ready_for_review`;
- a draft PR is not being intentionally ignored;
- the tunnel/reverse proxy is running;
- ingress forwards `POST`, raw body, and required headers;
- firewall policy allows GitHub delivery traffic;
- PullSage is listening on the address/port targeted by the proxy.

`127.0.0.1` is not reachable by GitHub. For local development, use an approved existing HTTPS tunnel and keep posting disabled.

## Webhook says ignored

An ignored response is normally deliberate. Inspect its safe sentence:

- `Unsupported GitHub event`: only `pull_request` is reviewed;
- `Unsupported pull-request action`: action is outside the supported four;
- `Draft pull requests are ignored until ready for review`: draft policy applied.

A duplicate response means either `GitHub delivery was already processed` or `An equivalent pull-request head is already queued`. Use a new synthetic delivery ID for a genuinely new local delivery test; do not enqueue duplicate work merely to bypass head deduplication.

Restarting to clear deduplication is not a production solution; let the cache expire or use a genuinely new delivery.

## Diff too large or too many changed files

### Symptoms

- job fails with a `pull_request_too_large` category;
- MCP diff/files tool reports a configured limit;
- result limitations say context was truncated.

### Relevant settings

```dotenv
PULLSAGE_MAX_DIFF_CHARS=200000
PULLSAGE_MAX_CHANGED_FILES=100
```

### Fix

- Split the PR into focused changes when possible.
- Review large/binary/generated files manually.
- Raise a limit only after evaluating memory, model context, latency, private-data exposure, and cost.
- Do not remove bounds.

Truncation can hide context. PullSage must disclose it and should not imply full review coverage.

## Review not posted because dry-run is enabled

This is the default safe behaviour.

Check capabilities:

```powershell
curl.exe http://127.0.0.1:8000/api/v1/config/capabilities
```

For automated webhook posting:

- `PULLSAGE_POST_COMMENTS` must be `true`;
- token must have pull-request write permission;
- validated output must survive confidence/path/line/verdict checks.

For a manual API review:

- request body must explicitly set `post_comments: true`;
- token must have pull-request write permission;
- the endpoint must be restricted to an authorized caller in production;
- `PULLSAGE_POST_COMMENTS` is not required for this explicit manual opt-in.

For direct MCP posting:

- call `pullsage_post_review`, not a read tool;
- `PULLSAGE_ALLOW_MCP_WRITE_TOOLS` must be `true`;
- restart the MCP process after changing the variable;
- the payload must be a valid structured PullSage review;
- token must have pull-request write permission.

An MCP `pullsage_review_pull_request` call with `post_comments=true` also requires `PULLSAGE_ALLOW_MCP_WRITE_TOOLS=true`.

Prefer review-first, human-inspect, explicit-post. Do not enable both write paths just to diagnose one.

## Review posting fails

If posting was authorized but GitHub rejected it:

- verify pull-request write permission;
- check organization/repository policies;
- confirm the PR is still open and the commit/line position is valid;
- inspect the safe GitHub status/category and request ID;
- check whether the authenticated user can submit the chosen event;
- ensure findings use changed right-side lines.

PullSage should move uncertain inline findings into the general body. It must not silently scatter retries or claim a post succeeded.

## Job stays in progress

Check:

- worker status in `/ready`;
- Codex timeout and process activity;
- GitHub latency/rate limiting;
- whether shutdown began;
- configured worker concurrency;
- redacted job-ID logs for the last transition.

Normal transitions are:

```text
queued -> fetching_context -> reviewing -> validating -> [posting] -> completed
```

Any stage may transition to `failed`.

If a worker crash leaves stale state, remember the MVP has no durable recovery. Restarting loses the in-memory job; production needs leases/heartbeats in a durable queue.

## Job ID returns `404`

Possible causes:

- typo or malformed UUID;
- job never existed in this process;
- API process restarted;
- completed/failed job exceeded `PULLSAGE_JOB_RETENTION_SECONDS`;
- request went to another replica with a separate memory store.

Do not deploy multiple API replicas behind round-robin load balancing and expect shared job polling. The MVP store is process-local.

## MCP server will not start

### Checks

```powershell
where.exe uv
uv run pullsage-mcp
codex mcp list
```

Also check:

- absolute `cwd` exists;
- the project is trusted by the host;
- project dependencies are available;
- stdout contains only MCP protocol output;
- desktop PATH/environment includes `uv`;
- there is no conflicting registration with the same name.

An idle direct launch is expected. STDIO servers wait for a host.

## MCP tool is unavailable or times out

- Restart the MCP host after changing `.codex/config.toml`.
- Confirm the server name is `pullsage`.
- Confirm tool names exactly:
  - `pullsage_get_pull_request`
  - `pullsage_get_changed_files`
  - `pullsage_get_pull_request_diff`
  - `pullsage_review_pull_request`
  - `pullsage_post_review`
- Increase the host tool timeout enough for the configured Codex attempt plus one possible repair attempt.
- Confirm the MCP server inherited `GITHUB_TOKEN`.
- For review, confirm the internal `CODEX_COMMAND` is visible to the MCP process.

Do not pass a token as a tool argument; no PullSage tool accepts one.

## API port already in use

PowerShell:

```powershell
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
```

Either stop the intended conflicting service or choose another port:

```powershell
$env:PULLSAGE_PORT = "8010"
uv run pullsage-api
```

Update the reverse proxy/webhook target accordingly. Do not bind to all interfaces merely to work around a local port issue.

## Windows command and path issues

### `curl` behaves like PowerShell web requests

Use `curl.exe` explicitly, or use `Invoke-RestMethod`/`Invoke-WebRequest` with PowerShell-native arguments.

### Paths contain spaces

Use one quoted PowerShell string:

```powershell
$env:CODEX_COMMAND = "C:\Program Files\Codex\codex.exe"
```

In TOML, forward slashes avoid backslash escaping:

```toml
cwd = "C:/Users/Example User/Desktop/PullSage"
```

Do not embed extra quote characters inside `CODEX_COMMAND`.

### GUI does not see environment changes

Environment variables set in PowerShell affect that process and its children, not an already-running desktop application. Fully restart the MCP host, or launch it from the configured environment.

### Execution policy blocks a wrapper

Prefer the installed executable path. Do not weaken machine-wide PowerShell execution policy as a troubleshooting shortcut.

### CRLF changes webhook signatures

Sign and send the same byte array. Do not read JSON as text, modify line endings, and then send different bytes.

## Dependencies are unavailable

If imports fail because dependencies have not been installed, do not fabricate packages or a lockfile. From the repository root, the operator can later run:

```powershell
uv sync --group dev
```

Then:

```powershell
uv run pytest
uv run ruff check .
uv run mypy src
```

`pyproject.toml` is authoritative. PullSage development should never trigger dependency installation implicitly.

## Collect a safe issue report

Include:

- concise reproduction;
- OS and Python/PullSage/Codex version;
- API versus MCP entry point;
- endpoint/tool name;
- HTTP status or MCP error category;
- request/job/delivery ID;
- safe readiness/capability flags;
- redacted logs around that ID;
- whether posting gates were enabled (boolean only);
- whether the repository is public or private (not its contents).

Exclude:

- `.env`;
- all tokens/secrets;
- authorization/cookie headers;
- Codex credential files;
- full webhook bodies from private repositories;
- complete private diffs/patches/prompts;
- private keys;
- screenshots containing any of the above.
