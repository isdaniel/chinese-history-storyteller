variable "subscription_id" {
  type        = string
  description = "Azure subscription ID. Get from: az account show --query id -o tsv. Required — pass via terraform.tfvars (gitignored) or TF_VAR_subscription_id."
}

variable "resource_group_name" {
  type    = string
  default = "rg-storyteller"
}

variable "resource_group_location" {
  type        = string
  description = "RG location. Doesn't have to match resource locations."
  default     = "eastus"
}

variable "openai_location" {
  type        = string
  description = "OpenAI region. Sweden Central is required for gpt-image-2 + gpt-5-mini."
  default     = "swedencentral"
}

variable "speech_location" {
  type    = string
  default = "eastus"
}

variable "storage_location" {
  type    = string
  default = "eastus"
}

variable "name_suffix" {
  type        = string
  description = "Stable suffix appended to globally-unique resource names. Used as-is for OpenAI/Speech; dashes are stripped for Storage Account (which only accepts lowercase + digits). Required — pass via terraform.tfvars."
}

variable "github_repo" {
  type        = string
  description = "OWNER/REPO used in GitHub Actions OIDC subjects. Required — pass via terraform.tfvars."
}

variable "current_user_object_id" {
  type        = string
  description = "Object ID of the developer user that needs local-dev access to Cognitive Services. Get from: az ad signed-in-user show --query id -o tsv. Required — pass via terraform.tfvars."
}
