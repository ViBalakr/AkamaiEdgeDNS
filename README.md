# Akamai Event Viewer → Microsoft Sentinel Collector

An Azure Function (Python 3.11, Flex Consumption) that retrieves administrative and DNS audit events from the Akamai Event Viewer API and ingests them into Microsoft Sentinel through the Azure Monitor Logs Ingestion API.

---

## Repository contents

This repository contains the Azure Function code that is deployed to Azure, and nothing else.

| File | Purpose |
|------|---------|
| `function_app.py` | Azure Functions Python v2 timer trigger. Runs every 5 minutes and orchestrates the collection run. |
| `akamai_client.py` | Signs and paginates Akamai Event Viewer API requests. Follows the `afterEventId` cursor and retries transient failures. |
| `edgegrid_auth.py` | EG1-HMAC-SHA256 request signing per the Akamai EdgeGrid specification. |
| `checkpoint.py` | Azure Blob-backed checkpoint with lease-based mutual exclusion. Tracks the last successfully ingested event ID. |
| `normalizer.py` | Maps Akamai Event Viewer JSON to the `AkamaiEdgeDNSEvents_CL` table schema. |
| `ingestion.py` | Logs Ingestion API client. Sends events to the DCE/DCR in configurable batches. |
| `config.py` | Reads all configuration from Function App settings (environment variables). No hardcoded values. |
| `logging_utils.py` | Structured logging helpers and execution metrics, promoted into Application Insights `customDimensions`. |
| `host.json` | Azure Functions host configuration. |
| `requirements.txt` | Python runtime dependencies. |

---

## How it works

Every 5 minutes the Function App:

1. Reads the checkpoint blob to find the last successfully ingested event ID.
2. Calls the Akamai Event Viewer API, signing each request with EdgeGrid.
3. Follows pagination until all events newer than the checkpoint are retrieved.
4. Normalizes each event to the shape of the Sentinel custom table.
5. Sends the events to the Logs Ingestion API in batches.
6. Advances the checkpoint only after ingestion succeeds.

If any step fails, the checkpoint is not advanced and the same events are retried on the next run. Delivery is **at-least-once**: no events are lost, but duplicates are possible in rare failure scenarios.

---

## Architecture

```
Akamai Event Viewer API
        │  HTTPS + EdgeGrid signature
        ▼
Azure Function App (Flex Consumption, Python 3.11)
        │  System-assigned managed identity
        ├──► Key Vault        (Akamai credentials)
        ├──► Blob Storage     (checkpoint.json)
        └──► DCE ──► DCR ──► AkamaiEdgeDNSEvents_CL (Sentinel workspace)
```

The checkpoint is stored in the same storage account that is created with the Function App, in the `akamai-checkpoint` container.

---

## Configuration

All configuration is supplied through Function App settings. Nothing is hardcoded.

### Required settings

| Setting | Example | Purpose |
|---------|---------|---------|
| `AKAMAI_BASE_URL` | `https://akab-xxxx.luna.akamaiapis.net` | Base URL of the Akamai Event Viewer API |
| `AKAMAI_HOST` | `akab-xxxx.luna.akamaiapis.net` | Host header; must match the base URL host |
| `AKAMAI_CLIENT_TOKEN` | `@Microsoft.KeyVault(SecretUri=...)` | EdgeGrid client token |
| `AKAMAI_ACCESS_TOKEN` | `@Microsoft.KeyVault(SecretUri=...)` | EdgeGrid access token |
| `AKAMAI_CLIENT_SECRET` | `@Microsoft.KeyVault(SecretUri=...)` | EdgeGrid client secret |
| `AZURE_STORAGE_ACCOUNT_URL` | `https://<storage>.blob.core.windows.net/` | Blob endpoint of the Function App's storage account |
| `DCE_ENDPOINT` | `https://<dce>.ingest.monitor.azure.com` | Logs ingestion endpoint of the DCE |
| `DCR_IMMUTABLE_ID` | `dcr-8a3b5c7d...` | Immutable ID of the Data Collection Rule |

### Optional settings

| Setting | Default | Purpose |
|---------|---------|---------|
| `AKAMAI_ACCOUNT_SWITCH_KEY` | *(empty)* | Only for accounts managing multiple customers |
| `CHECKPOINT_CONTAINER` | `akamai-checkpoint` | Container for the checkpoint blob |
| `CHECKPOINT_BLOB` | `checkpoint.json` | Checkpoint blob name |
| `DCR_STREAM_NAME` | `Custom-AkamaiEdgeDNSEvents_CL` | Stream name inside the DCR |
| `MAX_PAGES_PER_RUN` | `20` | Safety cap on API pagination per execution |
| `BATCH_SIZE` | `500` | Records per Logs Ingestion API call |
| `BOOTSTRAP_MODE` | `earliest` | `earliest` ingests history on first run; `latest` starts from the newest event |
| `LOG_LEVEL` | `INFO` | Python logging level |

### Platform settings

| Setting | Value | Note |
|---------|-------|------|
| `FUNCTIONS_EXTENSION_VERSION` | `~4` | Functions runtime v4 |
| `AzureWebJobsStorage__accountName` | *(storage account name)* | Identity-based storage for the runtime |
| `AzureWebJobsStorage__credential` | `managedidentity` | Never a connection string |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | *(App Insights connection string)* | Telemetry |

---

## Secrets management

The three Akamai credentials are stored in **Azure Key Vault**, never as plain app settings:

| Secret name | Contents |
|-------------|----------|
| `akamai-client-token` | Akamai EdgeGrid `client_token` |
| `akamai-access-token` | Akamai EdgeGrid `access_token` |
| `akamai-client-secret` | Akamai EdgeGrid `client_secret` |

App settings reference them with `@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/<name>/)`. Azure resolves the references at runtime using the Function App's managed identity, so the Python code never handles a raw credential.

To rotate a credential, update the secret value in Key Vault. The Function App picks it up within 24 hours, or immediately after a restart.

---

## Required permissions

The Function App's system-assigned managed identity needs three role assignments:

| Resource | Role |
|----------|------|
| Storage account (created with the Function App) | Storage Blob Data Contributor |
| Data Collection Rule | Monitoring Metrics Publisher |
| Key Vault | Key Vault Secrets User |

Role assignments can take up to 30 minutes to propagate. Until then, expect `403` errors.

---

## Prerequisites

- An existing Azure resource group and Microsoft Sentinel workspace
- An Akamai API client with **read-only** access to the Event Viewer API
- A custom table `AkamaiEdgeDNSEvents_CL` with its DCE and DCR
- Contributor access on the resource group and the ability to create role assignments

---

## Deployment

Deployment is manual, through the Azure Portal and Visual Studio Code. The full step-by-step procedure is in the accompanying **SOP (Standard Operating Procedure)**.

Summary:

1. Create the Key Vault and add the three Akamai secrets.
2. Create the custom Log Analytics table, DCE and DCR.
3. Create the Function App on a Flex Consumption plan (this also creates its storage account).
4. Enable the system-assigned managed identity.
5. Create the `akamai-checkpoint` container in the Function App's storage account.
6. Add the app settings, using Key Vault references for the Akamai credentials.
7. Assign the three roles above and wait for propagation.
8. Build the deployment ZIP and deploy it with VS Code.

### Building the deployment package

The package must contain exactly the ten files below at the ZIP root (no subfolders, no extra files):

```
akamai_client.py
checkpoint.py
config.py
edgegrid_auth.py
function_app.py
host.json
ingestion.py
logging_utils.py
normalizer.py
requirements.txt
```
---

## Runtime authentication

The code authenticates to Azure services with `DefaultAzureCredential()`. In Azure this resolves to the Function App's managed identity. No credentials, connection strings or SAS tokens are stored in code or settings.

For local development, `DefaultAzureCredential` falls back to your Azure CLI session (`az login`). No code change is needed between local and production.

---

## Failure semantics

| Failure point | Effect | Recovery |
|---------------|--------|----------|
| Akamai API call fails | Checkpoint not advanced | Next run retries the same events |
| Normalization fails | Checkpoint not advanced | Next run retries the same events |
| Sentinel ingestion fails | Checkpoint not advanced | Next run retries the same events |
| Checkpoint write fails after ingestion | Logged; events may be resent | At-least-once delivery; no data loss |

---

## Logging and monitoring

Logs go to Application Insights, wired to the same Log Analytics workspace as the Sentinel data.

Execution metrics:

```kql
AppTraces
| where Message startswith "Execution completed"
| extend metrics = parse_json(Properties)
| project TimeGenerated, metrics.events_sent, metrics.failure_count, metrics.execution_duration_seconds
```

Verify ingestion:

```kql
AkamaiEdgeDNSEvents_CL
| take 10
```

Live logs: Function App → **Log stream**.

---

## Troubleshooting

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| `403 AuthorizationPermissionMismatch` | Role assignment not yet propagated | Wait 30 minutes, then restart the Function App |
| `401` from Akamai | Wrong or rotated credentials | Check the Key Vault secret values |
| Key Vault reference shows a red icon | Missing role, wrong secret name, or missing trailing slash | Check the role assignment and the reference syntax |
| `ConfigurationError: Missing required configuration value` | App setting missing | Compare against the settings tables above |
| No functions listed after deployment | ZIP has a folder prefix or extra files | Rebuild the ZIP with exactly ten files at the root |

---

## Contributing

Do not commit:

- `local.settings.json`
- `.env` files
- Any file containing an Akamai credential
- Test, mock or scratch files

Suggested `.gitignore`:

```
local.settings.json
.env
.venv/
__pycache__/
*.zip
.vscode/
```

## Support

For collector issues, contact the team that owns this repository. For Akamai credential or API access issues, contact your Akamai Control Center administrator.
