# Set GitHub Actions secrets for the chinese-history-storyteller repo from Terraform outputs.
#
# Usage (PowerShell):
#   1. Install gh CLI: winget install --id GitHub.cli   (then RESTART PowerShell)
#   2. cd C:\gitRepo\chinese-history-storyteller\infra
#   3. gh auth login                                    (if not logged in)
#   4. ..\scripts\set_github_secrets.ps1
#
# Re-run any time terraform outputs change (e.g. after key rotation).

param(
    [string]$Repo = "isdaniel/chinese-history-storyteller"
)

$ErrorActionPreference = "Stop"

# Sanity check
$tfDir = Join-Path $PSScriptRoot "..\infra" | Resolve-Path
Push-Location $tfDir
try {
    Write-Host "Reading terraform outputs from $tfDir ..." -ForegroundColor Cyan

    # tf-output helper
    function Tf-Out([string]$name) {
        $v = & terraform output -raw $name 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $v) {
            throw "terraform output '$name' is empty or failed"
        }
        return $v
    }

    # Helper to set a secret
    function Set-Secret([string]$name, [string]$value) {
        if (-not $value) {
            Write-Host "  SKIP $name (empty)" -ForegroundColor Yellow
            return
        }
        $value | gh secret set $name -R $Repo
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  OK   $name" -ForegroundColor Green
        } else {
            Write-Host "  FAIL $name" -ForegroundColor Red
        }
    }

    Write-Host "`nSetting Azure secrets on $Repo ..." -ForegroundColor Cyan
    Set-Secret "AZURE_CLIENT_ID"               (Tf-Out "azure_client_id")
    Set-Secret "AZURE_TENANT_ID"               (Tf-Out "azure_tenant_id")
    Set-Secret "AZURE_SUBSCRIPTION_ID"         (Tf-Out "azure_subscription_id")
    Set-Secret "AZURE_OPENAI_ENDPOINT"         (Tf-Out "azure_openai_endpoint")
    Set-Secret "AZURE_OPENAI_GPT_DEPLOYMENT"   (Tf-Out "azure_openai_gpt_deployment")
    Set-Secret "AZURE_OPENAI_IMAGE_DEPLOYMENT" (Tf-Out "azure_openai_image_deployment")
    Set-Secret "AZURE_OPENAI_API_VERSION"      "2024-10-21"
    Set-Secret "AZURE_SPEECH_REGION"           (Tf-Out "azure_speech_region")
    Set-Secret "AZURE_SPEECH_RESOURCE_ID"      (Tf-Out "azure_speech_resource_id")
    Set-Secret "AZURE_SPEECH_CUSTOM_DOMAIN"    (Tf-Out "azure_speech_custom_domain")
    Set-Secret "AZURE_SPEECH_VOICE"            "zh-CN-YunjianNeural"
    Set-Secret "AZURE_STORAGE_CONNECTION_STRING" (Tf-Out "azure_storage_connection_string")
    Set-Secret "AZURE_STORAGE_CONTAINER"       (Tf-Out "azure_storage_container")
    Set-Secret "AZURE_BLOB_PUBLIC_URL_BASE"    (Tf-Out "azure_blob_public_url_base")

    Write-Host "`nDone. Run 'gh secret list -R $Repo' to verify." -ForegroundColor Cyan
} finally {
    Pop-Location
}
