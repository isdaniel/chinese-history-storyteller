output "azure_openai_endpoint" {
  value = azurerm_cognitive_account.openai.endpoint
}

output "azure_openai_gpt_deployment" {
  value = azurerm_cognitive_deployment.gpt5_mini.name
}

output "azure_openai_image_deployment" {
  value = azurerm_cognitive_deployment.gpt_image_2.name
}

output "azure_speech_region" {
  value = azurerm_cognitive_account.speech.location
}

output "azure_speech_resource_id" {
  value = azurerm_cognitive_account.speech.id
}

output "azure_speech_custom_domain" {
  description = "Custom subdomain name (without .cognitiveservices.azure.com). Required for Entra ID issueToken."
  value       = azurerm_cognitive_account.speech.custom_subdomain_name
}

output "azure_storage_account_name" {
  value = azurerm_storage_account.podcast.name
}

output "azure_storage_container" {
  value = azurerm_storage_container.podcast_episodes.name
}

output "azure_storage_connection_string" {
  description = "Set as GitHub secret AZURE_STORAGE_CONNECTION_STRING"
  value       = azurerm_storage_account.podcast.primary_connection_string
  sensitive   = true
}

output "azure_blob_public_url_base" {
  value = "https://${azurerm_storage_account.podcast.name}.blob.core.windows.net/${azurerm_storage_container.podcast_episodes.name}"
}

output "azure_client_id" {
  description = "Set this as GitHub secret AZURE_CLIENT_ID"
  value       = azuread_application.github.client_id
}

output "azure_tenant_id" {
  value = azuread_service_principal.github.application_tenant_id
}

output "azure_subscription_id" {
  value = var.subscription_id
}
