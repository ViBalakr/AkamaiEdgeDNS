# Akamai Event Viewer -> Microsoft Sentinel Collector (Production Package)

This repository contains **only** the production Azure Function code for
the Akamai Event Viewer -> Microsoft Sentinel collector. It exists to be
tagged and released (`git tag vX.Y.Z && git push origin vX.Y.Z`), producing
a `collector-release.zip` GitHub Release asset that the companion ARM
template (`azuredeploy-github.json`, in the deployment repository/package)
pulls down and deploys to the Azure Function App it creates.

## What's here

| File | Purpose |
|---|---|
| `function_app.py` | Azure Functions Python v2 timer trigger (`0 */5 * * * *`) - orchestrates the collection run |
| `akamai_client.py` | Signs and paginates Akamai Event Viewer API requests |
| `edgegrid_auth.py` | EG1-HMAC-SHA256 EdgeGrid request signing |
| `checkpoint.py` | Azure Blob-backed checkpoint with lease locking |
| `normalizer.py` | Maps Akamai Event Viewer JSON to the `AkamaiEdgeDNSEvents_CL` schema |
| `ingestion.py` | Azure Monitor Logs Ingestion API client |
| `config.py` | Reads all configuration from Function App settings (environment variables) |
| `logging_utils.py` | Structured logging + execution metrics (promoted into Application Insights `customDimensions`) |
| `host.json` | Azure Functions host configuration |
| `requirements.txt` | Python dependencies |

## What's deliberately NOT here

No mock Akamai server, no synthetic test data, no test suite. This is a
**production-only** repository. Local development and testing happen in a
separate lab repository/package that additionally contains
`mock_akamai.py`, `mock_data.py`, and `tests/` - none of which should ever
be present here (the release workflow below refuses to build if they are).

## Configuration

This code reads all configuration from environment variables / Function
App settings - nothing is hardcoded. See the companion ARM template's
parameter list and `Microsoft.Web/sites/config` resource for the complete
set (`AKAMAI_BASE_URL`, `AKAMAI_HOST`, `AKAMAI_CLIENT_TOKEN`,
`AKAMAI_ACCESS_TOKEN`, `AKAMAI_CLIENT_SECRET`, `DCE_ENDPOINT`,
`DCR_IMMUTABLE_ID`, `DCR_STREAM_NAME`, `AZURE_STORAGE_ACCOUNT_URL`,
`CHECKPOINT_CONTAINER`, `CHECKPOINT_BLOB`, `MAX_PAGES_PER_RUN`,
`BATCH_SIZE`, `BOOTSTRAP_MODE`, `LOG_LEVEL`). Azure authentication uses
`DefaultAzureCredential()` throughout, resolving to the Function App's
System Assigned Managed Identity in Azure.

## Releasing a new version

Push a tag matching `v*`:

```
git tag v1.0.0
git push origin v1.0.0
```

`.github/workflows/release.yml` then automatically:

1. Refuses to build if any lab/mock/test file is present (guard rail).
2. Verifies all 10 required production files exist.
3. Syntax-checks every Python file (`py_compile`) and validates `host.json`.
4. Builds `collector-release.zip` (the 10 files above, flat - no
   subdirectories, matching what Azure Functions expects at the root of
   the deployment package).
5. Publishes a GitHub Release with that zip attached, printing its
   SHA-256 in the workflow log for manual verification if you want it.

The resulting asset URL - `https://github.com/<org>/<repo>/releases/download/v1.0.0/collector-release.zip`
- is what you supply to the ARM template's `collectorPackageUrl` parameter.

## Private repository?

If this repository is private (recommended for most organizations - there
is no reason SIEM connector code needs to be public even though it
contains no secrets), release asset downloads require an
`Authorization: Bearer <token>` header. The companion ARM template
supports this via an optional `githubToken` secure parameter - see
DEPLOY-NOTES-GITHUB.md "Private repository access" for the exact token
scope needed (a fine-grained PAT with only `contents: read` on this one
repository is sufficient - never a classic PAT with broader scope).
