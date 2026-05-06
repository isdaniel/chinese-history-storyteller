locals {
  openai_name  = "openai-storyteller-${var.name_suffix}"
  speech_name  = "speech-storyteller-${var.name_suffix}"
  storage_name = "stgstoryteller${replace(var.name_suffix, "-", "")}"
  app_name     = "github-storyteller-pipeline-${var.name_suffix}"

  storage_container = "podcast-episodes"

  fic_subjects = {
    main       = "repo:${var.github_repo}:ref:refs/heads/main"
    production = "repo:${var.github_repo}:environment:production"
    # NOTE: do NOT add a `pull_request` subject for a public repo — anyone could
    # open a PR and exchange OIDC for an Azure token with our RBAC.
  }

  role_def = {
    cognitive_services_openai_user = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
    cognitive_services_user        = "a97b65f3-24c7-4388-baec-2e87135dc908"
  }
}

# -----------------------------------------------------------------------------
# Resource Group
# -----------------------------------------------------------------------------
resource "azurerm_resource_group" "main" {
  name     = var.resource_group_name
  location = var.resource_group_location
}

# -----------------------------------------------------------------------------
# Azure OpenAI (Sweden Central)
# -----------------------------------------------------------------------------
resource "azurerm_cognitive_account" "openai" {
  name                  = local.openai_name
  resource_group_name   = azurerm_resource_group.main.name
  location              = var.openai_location
  kind                  = "OpenAI"
  sku_name              = "S0"
  custom_subdomain_name = local.openai_name
  local_auth_enabled    = false
}

resource "azurerm_cognitive_deployment" "gpt5_mini" {
  name                 = "gpt-5-mini"
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = "gpt-5-mini"
    version = "2025-08-07"
  }

  sku {
    name     = "GlobalStandard"
    capacity = 10
  }
}

resource "azurerm_cognitive_deployment" "gpt_image_2" {
  name                 = "gpt-image-2"
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = "gpt-image-2"
    version = "2026-04-21"
  }

  sku {
    name     = "GlobalStandard"
    capacity = 2
  }
}

# -----------------------------------------------------------------------------
# Azure Speech (East US, F0)
# -----------------------------------------------------------------------------
resource "azurerm_cognitive_account" "speech" {
  name                  = local.speech_name
  resource_group_name   = azurerm_resource_group.main.name
  location              = var.speech_location
  kind                  = "SpeechServices"
  sku_name              = "F0"
  custom_subdomain_name = local.speech_name
  local_auth_enabled    = false
}

# -----------------------------------------------------------------------------
# Azure Storage (LRS Hot) — hosts podcast mp3 enclosures
# -----------------------------------------------------------------------------
resource "azurerm_storage_account" "podcast" {
  name                          = local.storage_name
  resource_group_name           = azurerm_resource_group.main.name
  location                      = var.storage_location
  account_tier                  = "Standard"
  account_replication_type      = "LRS"
  account_kind                  = "StorageV2"
  access_tier                   = "Hot"
  allow_nested_items_to_be_public = true
  public_network_access_enabled   = true
  https_traffic_only_enabled      = true
  min_tls_version                 = "TLS1_2"

  blob_properties {
    cors_rule {
      allowed_origins    = ["*"]
      allowed_methods    = ["GET", "HEAD"]
      allowed_headers    = ["*"]
      exposed_headers    = ["*"]
      max_age_in_seconds = 3600
    }
  }
}

resource "azurerm_storage_container" "podcast_episodes" {
  name                  = local.storage_container
  storage_account_id    = azurerm_storage_account.podcast.id
  # "blob" = anonymous read by exact URL only (no listing). Required for podcast
  # apps to fetch enclosure URLs. Don't use "container" — that would expose blob
  # listing and let anyone enumerate every episode.
  container_access_type = "blob"
}

# -----------------------------------------------------------------------------
# Entra ID App Registration for GitHub Actions OIDC
# -----------------------------------------------------------------------------
resource "azuread_application" "github" {
  display_name = local.app_name

  lifecycle {
    ignore_changes = [owners, api]
  }
}

resource "azuread_service_principal" "github" {
  client_id = azuread_application.github.client_id

  lifecycle {
    ignore_changes = [owners]
  }
}

resource "azuread_application_federated_identity_credential" "github" {
  for_each = local.fic_subjects

  application_id = azuread_application.github.id
  display_name   = replace(replace(each.value, ":", "-"), "/", "-")
  description    = "GitHub Actions for ${var.github_repo}"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = each.value
}

# -----------------------------------------------------------------------------
# RBAC role assignments (use azapi to bypass az-cli MissingSubscription bug)
# -----------------------------------------------------------------------------
resource "azurerm_role_assignment" "sp_openai_user" {
  scope                = azurerm_cognitive_account.openai.id
  role_definition_id   = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${local.role_def.cognitive_services_openai_user}"
  principal_id         = azuread_service_principal.github.object_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "sp_speech_user" {
  scope                = azurerm_cognitive_account.speech.id
  role_definition_id   = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${local.role_def.cognitive_services_user}"
  principal_id         = azuread_service_principal.github.object_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "me_openai_user" {
  scope                = azurerm_cognitive_account.openai.id
  role_definition_id   = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${local.role_def.cognitive_services_openai_user}"
  principal_id         = var.current_user_object_id
  principal_type       = "User"
}

resource "azurerm_role_assignment" "me_speech_user" {
  scope                = azurerm_cognitive_account.speech.id
  role_definition_id   = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${local.role_def.cognitive_services_user}"
  principal_id         = var.current_user_object_id
  principal_type       = "User"
}
