terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
  }

  # Remote state — Entra ID auth, shared key disabled on the storage account.
  # Bootstrap separately (rg-storyteller-tfstate) so a `terraform destroy` of
  # the main resources never wipes its own state.
  backend "azurerm" {
    resource_group_name  = "rg-storyteller-tfstate"
    storage_account_name = "stgsttftateaihistory"
    container_name       = "tfstate"
    key                  = "storyteller.tfstate"
    use_azuread_auth     = true
  }
}

provider "azurerm" {
  subscription_id = var.subscription_id
  features {}
}

provider "azuread" {}

provider "azapi" {}
