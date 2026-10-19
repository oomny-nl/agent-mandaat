output "blueprint_app_id" {
  description = "client_id van de blueprint (stap 2 van de agent-OBO-flow)."
  value       = msgraph_resource.blueprint.output.app_id
}

output "blueprint_scope" {
  description = "Scope waarmee de werkplek het token van de medewerker (Tc) voor stap 3 vraagt."
  value       = "api://${msgraph_resource.blueprint.output.app_id}/access_agent"
}

output "agent_identity_id" {
  description = "client_id van de agent identity (fmi_path in stap 2, client_id in stap 3)."
  # Bij een agent identity is appId gelijk aan het object-id; Graph geeft appId niet terug bij het
  # aanmaken.
  value = msgraph_resource.agent.id
}

output "managed_identity_client_id" {
  description = "client_id van de managed identity die de blueprint authenticeert (stap 1)."
  value       = azapi_resource.identiteit.output.client_id
}

output "dossier_api_app_id" {
  description = "Audience (aud) van de tokens voor de dossier-API."
  value       = msgraph_resource.dossier_api.output.app_id
}

output "dossier_api_scope_lezen" {
  description = "Voorbeeld van een scope voor stap 3; notitie.schrijven gaat op dezelfde manier."
  value       = "api://${msgraph_resource.dossier_api.output.app_id}/dossier.lezen"
}
