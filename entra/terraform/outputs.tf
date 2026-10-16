output "blueprint_app_id" {
  description = "client_id van de blueprint (stap 1 van de agent-OBO-flow)."
  value       = msgraph_resource.blueprint.output.app_id
}

output "agent_identity_id" {
  description = "client_id van de agent identity (fmi_path in stap 1, client_id in stap 2)."
  value       = msgraph_resource.agent.output.app_id
}

output "managed_identity_client_id" {
  description = "client_id van de managed identity die de blueprint authenticeert."
  value       = azapi_resource.identiteit.output.client_id
}

output "dossier_api_app_id" {
  description = "Audience van de dossier-API."
  value       = msgraph_resource.dossier_api.output.app_id
}
