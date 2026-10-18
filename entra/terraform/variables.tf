variable "tenant_id" {
  description = "Entra-tenant waarin de agent en de dossier-API leven."
  type        = string
}

variable "subscription_id" {
  description = "Azure-subscription voor de resourcegroep en de managed identity."
  type        = string
}

variable "locatie" {
  description = "Azure-regio; standaard West-Europa (Nederland)."
  type        = string
  default     = "westeurope"
}

variable "sponsor_object_id" {
  description = "Object-id van de sponsor: de gebruiker die verantwoordelijk is voor de agent."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-fA-F-]{36}$", var.sponsor_object_id))
    error_message = "sponsor_object_id moet de object-id (GUID) van een gebruiker zijn."
  }
}

variable "eigenaar_object_id" {
  description = "Object-id van de gebruiker die eigenaar wordt en de blueprint mag beheren."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-fA-F-]{36}$", var.eigenaar_object_id))
    error_message = "eigenaar_object_id moet de object-id (GUID) van een gebruiker zijn."
  }
}

variable "naam" {
  description = "Basisnaam voor alle objecten."
  type        = string
  default     = "lindevoort-dossierassistent"
}
