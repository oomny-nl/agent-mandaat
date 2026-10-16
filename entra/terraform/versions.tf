terraform {
  required_version = ">= 1.9"
  required_providers {
    msgraph = {
      source  = "microsoft/msgraph"
      version = "0.6.0"
    }
    azapi = {
      source  = "azure/azapi"
      version = "2.13.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.9.1"
    }
  }
}

provider "msgraph" {}
provider "azapi" {}
