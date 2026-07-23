# GitHub setup

## Overview

The PullSage MVP uses:

- one `GITHUB_TOKEN` for GitHub REST API calls;
- one independent `GITHUB_WEBHOOK_SECRET` for HMAC verification;
- the `pull_request` webhook event;
- HTTPS REST calls to `GITHUB_API_URL`, which defaults to `https://api.github.com`.

The token and webhook secret serve different purposes and must not be the same value. Neither belongs in source control, Codex prompts, MCP project configuration, URLs, or logs.

## Choose a token

A fine-grained personal access token is the preferred MVP credential because it can be restricted to selected repositories and permissions.

### Repository access

Select only the owner and repositories that this PullSage instance must review. Avoid an all-repositories token for a local MVP.

### Minimum permissions

For dry-run inspection and review generation:

| Permission | Access |
| --- | --- |
| Metadata | Read |
| Pull requests | Read |

To let PullSage submit a review:

| Permission | Access |
| --- | --- |
| Metadata | Read |
| Pull requests | Read and write |

GitHub may make metadata-read implicit. No contents-write, administration, workflows, issues, checks, deployments, or organization-management permission is required by PullSage.

If an organization enforces SSO or token approval, authorize/approve the token according to that organization's policy. A token can be syntactically valid yet receive `403` for an unapproved organization or inaccessible repository.

Classic personal access tokens are broader and are not recommended. If organizational constraints require one, use the narrowest policy permitted and understand that the classic `repo` scope is substantially wider than PullSage needs.

## Store the token

For an untracked local `.env`:

```dotenv
GITHUB_TOKEN=replace-with-a-token-from-your-secret-store
```

The committed `.env.example` contains placeholders only. Verify `.env` is ignored before placing any credential in it.

For one PowerShell session, preferably reading from an OS-protected secret source:

```powershell
$env:GITHUB_TOKEN = (Get-Content "$env:USERPROFILE\.secrets\pullsage-github-token.txt" -Raw).Trim()
uv run pullsage-api
```

For the MCP server, start the MCP host from the protected environment so the child process inherits `GITHUB_TOKEN`. Do not commit a token to `.codex/config.toml`.

On macOS/Linux, use your shell's environment injection or secret manager. Be aware that literal `export GITHUB_TOKEN=...` commands may remain in shell history.

### Token hygiene

- Use a dedicated credential rather than a personal general-purpose token.
- Set an expiration and rotate it.
- Revoke immediately if it appears in a commit, log, screenshot, terminal transcript, or issue.
- Keep posting disabled until read-only access has been verified.
- Do not test redaction by deliberately printing a real token.

## Configure the API URL

For GitHub.com:

```dotenv
GITHUB_API_URL=https://api.github.com
```

For GitHub Enterprise Server, use the instance's REST API base URL, commonly:

```dotenv
GITHUB_API_URL=https://github.example.com/api/v3
```

Confirm the exact URL and TLS trust policy with the instance administrator. Do not disable certificate verification to work around a development certificate; configure the host trust store correctly.

## Create the webhook secret

Create a high-entropy random value in a trusted secret-management workflow. The secret should:

- be unique to this webhook/PullSage deployment;
- be unrelated to `GITHUB_TOKEN`;
- be available to the FastAPI process as `GITHUB_WEBHOOK_SECRET`;
- never be sent to Codex or returned by an endpoint;
- be rotated by updating GitHub and the service in a coordinated window.

Example placeholder:

```dotenv
GITHUB_WEBHOOK_SECRET=replace-with-an-independent-random-secret
```

Do not copy the placeholder into production unchanged.

## Add the repository webhook

In the GitHub repository settings, add a webhook with:

| Setting | Value |
| --- | --- |
| Payload URL | `https://pullsage.example.com/webhooks/github` |
| Content type | `application/json` |
| Secret | The value injected as `GITHUB_WEBHOOK_SECRET` |
| SSL verification | Enabled |
| Events | Select individual events: **Pull requests** |
| Active | Enabled |

For organization-wide installation, each selected repository needs a webhook in the MVP. The future GitHub App model provides a cleaner organization/repository installation boundary.

The payload URL must be reachable by GitHub. `127.0.0.1` and a private workstation address are not reachable from GitHub-hosted delivery infrastructure.

## Supported deliveries

PullSage accepts the `pull_request` event with these actions:

- `opened`
- `reopened`
- `synchronize`
- `ready_for_review`

All other webhook event types and pull-request actions are acknowledged and ignored. Draft pull requests are ignored unless the action is `ready_for_review`.

Required delivery headers:

- `X-Hub-Signature-256`
- `X-GitHub-Event`
- `X-GitHub-Delivery`

The signature must use the `sha256=` form. PullSage computes HMAC SHA-256 over the exact raw body and compares with `hmac.compare_digest` before parsing JSON.

The delivery ID is held in a bounded, expiring memory cache controlled by `PULLSAGE_DELIVERY_RETENTION_SECONDS` (default `3600`) and `PULLSAGE_MAX_WEBHOOK_DELIVERIES` (default `10000`). This reduces accidental duplicate processing while one API process remains alive, but it is not durable exactly-once delivery. Restarting PullSage clears the cache.

## Local development

### Start in safe mode

Use loopback binding and keep both write gates off:

```dotenv
PULLSAGE_HOST=127.0.0.1
PULLSAGE_POST_COMMENTS=false
PULLSAGE_ALLOW_MCP_WRITE_TOOLS=false
```

Then run:

```powershell
uv run pullsage-api
```

Check:

```powershell
curl.exe http://127.0.0.1:8000/health
curl.exe http://127.0.0.1:8000/ready
```

Readiness distinguishes configuration/executable problems without exposing credentials.

### Receive real GitHub deliveries

Use an organization-approved HTTPS tunnel or reverse proxy that is already installed. Forward it to `http://127.0.0.1:8000`, then use:

```text
https://assigned-tunnel-host.example/webhooks/github
```

as the payload URL.

Safety notes:

- A tunnel makes the local endpoint internet-accessible.
- Always configure the webhook secret first.
- Keep GitHub posting disabled while testing ingress.
- Limit the token to a disposable test repository if possible.
- Do not expose `/docs`, manual review routes, or job data broadly without authentication.
- Stop the tunnel when finished.
- Do not run an installer copied from a website without your organization's review.

### Offline signature-path test on Windows

Create `payload.json` containing synthetic webhook data. Use a disposable local secret and sign the exact bytes you send:

```powershell
$env:GITHUB_WEBHOOK_SECRET = "local-test-only-secret"
$payloadPath = (Resolve-Path .\payload.json).Path
$body = [System.IO.File]::ReadAllBytes($payloadPath)
$key = [Text.Encoding]::UTF8.GetBytes($env:GITHUB_WEBHOOK_SECRET)
$hmac = [Security.Cryptography.HMACSHA256]::new($key)
$signature = "sha256=" + [Convert]::ToHexString($hmac.ComputeHash($body)).ToLowerInvariant()

Invoke-WebRequest `
  -Method Post `
  -Uri http://127.0.0.1:8000/webhooks/github `
  -ContentType application/json `
  -Headers @{
    "X-Hub-Signature-256" = $signature
    "X-GitHub-Event" = "pull_request"
    "X-GitHub-Delivery" = [guid]::NewGuid().ToString()
  } `
  -Body $body

Remove-Item Env:\GITHUB_WEBHOOK_SECRET
```

Do not parse and re-serialize the payload between signing and sending; whitespace or newline changes alter the signature.

### Test from GitHub

The webhook's **Recent deliveries** view shows request/response information and supports redelivery. Use a new delivery for ordinary deduplication tests; a redelivery may be intentionally identified as a duplicate while its cache entry remains live.

Record the delivery ID and PullSage request/job ID for correlation. Do not paste the payload from a private repository into a public bug report.

## Enable review posting intentionally

First verify dry-run results and grant pull-request write permission only to the intended credential.

For automated webhook reviews, set:

```dotenv
PULLSAGE_POST_COMMENTS=true
```

For a manual API review, the request itself is the explicit opt-in. Its safe default remains:

```json
{
  "owner": "octo-org",
  "repository": "example",
  "pull_request_number": 42,
  "post_comments": false
}
```

Change `post_comments` to `true` only for a deliberate, authorized manual write. That explicit manual value can post independently of `PULLSAGE_POST_COMMENTS`, so keep the endpoint on loopback or protect it with authentication and repository authorization before production exposure.

PullSage prefers one GitHub review submission, filters low-confidence and duplicate findings, and keeps unsafe line mappings in the general body.

`PULLSAGE_ALLOW_MCP_WRITE_TOOLS` is separate and does not need to be enabled for webhook/manual API posting. It is required for both MCP review calls with `post_comments=true` and the direct MCP post tool. Leave it off unless MCP posting is specifically required.

## Required GitHub REST operations

The credential must be able to call:

```text
GET  /repos/{owner}/{repo}/pulls/{pull_number}
GET  /repos/{owner}/{repo}/pulls/{pull_number}/files
GET  /repos/{owner}/{repo}/pulls/{pull_number}   (diff media type)
POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews   (only when enabled)
```

PullSage sends an API version header and appropriate media type. Changed files are paginated. GitHub errors are mapped to safe authentication, rate-limit, not-found, general API, or posting failures; authorization headers are not logged.

## Troubleshoot setup

### GitHub returns `401`

- Confirm the token is present in the environment of the PullSage process.
- Confirm it has not expired or been revoked.
- Remove trailing newlines if loading from a file.
- Restart PullSage after changing its environment.
- Never print the token as a diagnostic.

### GitHub returns `403`

- Confirm the token can access the selected owner/repository.
- Check organization approval and SSO authorization.
- Check rate-limit details in the safe PullSage error/log metadata.
- For posting, confirm pull-request write permission and repository policy.

### GitHub returns `404`

GitHub can use `404` to avoid revealing a private resource. Check spelling, owner, PR number, repository selection, and token access.

### Signature verification fails

- Confirm the GitHub webhook secret and `GITHUB_WEBHOOK_SECRET` are byte-for-byte equal.
- Confirm the webhook content type is `application/json`.
- Preserve raw bytes through proxies/middleware.
- Ensure the `sha256=` signature header reaches PullSage unchanged.
- Do not confuse the webhook secret with the GitHub token.

### No delivery appears

- Confirm the webhook is active and subscribed to pull requests.
- Confirm the action is supported.
- Confirm the PR is not still a draft.
- Inspect GitHub Recent deliveries for DNS, TLS, timeout, and response data.
- Confirm the public URL ends with `/webhooks/github`.

See [Troubleshooting](troubleshooting.md) for more.

## Production roadmap: GitHub App authentication

A GitHub App is the preferred production credential model because it provides installation-scoped access and short-lived installation tokens.

The intended evolution is:

1. Register a GitHub App with pull-request read permission and optional pull-request write permission.
2. Subscribe the app to `pull_request`.
3. Verify webhooks with the app webhook secret exactly as the MVP does.
4. Store the app private key in a managed secret service, never in the repository.
5. Validate the GitHub App webhook installation/repository identity.
6. Create a signed app JWT only in the credential provider.
7. exchange it for a short-lived installation token;
8. cache installation tokens only until shortly before expiry;
9. select credentials per installation/repository request;
10. persist delivery/idempotency and write-audit records in a durable store.

The future credential provider should satisfy the GitHub client's authentication interface. Review logic, validation, API routes, and MCP tools should not need GitHub App-specific branches.

Production also needs explicit authorization for manual API and remote MCP callers. Possessing a repository identifier must never be sufficient to spend an installation token or post a review.
