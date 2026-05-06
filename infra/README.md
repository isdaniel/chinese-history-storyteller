# Terraform — chinese-history-storyteller infrastructure

Manages all Azure + Entra ID resources used by the pipeline.

## What's here

- `azurerm_resource_group.main` — `rg-storyteller`
- `azurerm_cognitive_account.openai` — Azure OpenAI in Sweden Central, local auth disabled
- `azurerm_cognitive_deployment.gpt5_mini` — `gpt-5-mini` 2025-08-07, GlobalStandard 10
- `azurerm_cognitive_deployment.gpt_image_2` — `gpt-image-2` 2026-04-21, GlobalStandard 2
- `azurerm_cognitive_account.speech` — Speech F0 in East US
- `azurerm_storage_account.podcast` — LRS Hot, hosts podcast mp3 enclosures
- `azurerm_storage_container.podcast_episodes` — `blob` access (anonymous read by URL, no listing)
- `azuread_application.github` + service principal — used by GitHub Actions via OIDC
- 2 federated identity credentials — `main`, `production` (no `pull_request` — public-repo footgun)
- 4 role assignments — SP and current user get `Cognitive Services OpenAI User` on OpenAI and `Cognitive Services User` on Speech

## Remote state

State is stored in Azure Storage, not in this repo:

- RG: `rg-storyteller-tfstate` (separate from `rg-storyteller` so `terraform destroy` of the main resources never wipes its own state)
- Storage account: `stgsttftateaihistory` (shared key disabled, public access disabled, TLS 1.2)
- Container: `tfstate`
- Blob: `storyteller.tfstate`
- Auth: Entra ID (`use_azuread_auth = true`) — no storage keys touch your shell

You need `Storage Blob Data Owner` (or Contributor) on the state storage account to read/write state. The GitHub Actions SP is granted Contributor too.

## Bootstrap from scratch (new tenant or fresh clone)

If the state storage doesn't exist yet (first time setup):

```bash
SUB=$(az account show --query id -o tsv)
ME=$(az ad signed-in-user show --query id -o tsv)
SA=stgsttftateaihistory   # or your own globally-unique name (≤24 lowercase+digits)

az group create -n rg-storyteller-tfstate -l eastus
az storage account create -n $SA -g rg-storyteller-tfstate -l eastus \
  --sku Standard_LRS --kind StorageV2 --access-tier Hot \
  --allow-blob-public-access false --allow-shared-key-access false \
  --min-tls-version TLS1_2 --https-only true

# Grant yourself blob data ownership
SA_ID=$(az storage account show -n $SA -g rg-storyteller-tfstate --query id -o tsv)
ROLE="/subscriptions/$SUB/providers/Microsoft.Authorization/roleDefinitions/b7e6dc6d-f1e8-4753-8033-0f276bb0955b"
GUID=$(python -c "import uuid; print(uuid.uuid4())")
az rest --method PUT \
  --url "https://management.azure.com${SA_ID}/providers/Microsoft.Authorization/roleAssignments/${GUID}?api-version=2022-04-01" \
  --body "{\"properties\":{\"roleDefinitionId\":\"$ROLE\",\"principalId\":\"$ME\",\"principalType\":\"User\"}}"

# Wait 30s for RBAC, then create container
sleep 30
az storage container create -n tfstate --account-name $SA --auth-mode login
```

If your storage account name differs from `stgsttftateaihistory`, update the `backend "azurerm"` block in `versions.tf`.

## Deploy

1. **Sign in** to the target tenant: `az login --tenant <tenant-id>`
2. **Copy** `terraform.tfvars.example` → `terraform.tfvars` and fill in real values (this file is gitignored).
3. **Apply**:
   ```bash
   cd infra
   terraform init
   terraform plan -out tfplan
   terraform apply tfplan
   ```
4. Read `terraform output` for GitHub secret values (see `MANUAL_SETUP.md`).

## Notes

- `local_auth_enabled = false` matches a typical tenant policy that disables Cognitive Services API keys.
- gpt-image-2 only exists in Sweden Central / East US 2 / West US 3.
- `container_access_type = "blob"` allows anonymous read by URL but no listing — required for podcast apps to fetch enclosures without exposing the full episode index.
- Federated credential subjects intentionally do NOT include `pull_request` — that would let any forked PR exchange OIDC for an Azure token with our RBAC, on a public repo this is a critical risk.
