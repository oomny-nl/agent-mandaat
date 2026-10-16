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
  description = "Object-id van de sponsor: de mens of groep die verantwoordelijk is voor de agent."
  type        = string
}

variable "eigenaar_object_id" {
  description = "Object-id van de eigenaar die de blueprint mag beheren."
  type        = string
}

variable "naam" {
  description = "Basisnaam voor alle objecten."
  type        = string
  default     = "lindevoort-dossierassistent"
}
