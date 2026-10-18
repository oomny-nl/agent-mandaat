# Entra Agent ID-variant van de dossierassistent.
#
# Niet tegen een tenant gevalideerd: CI draait alleen `terraform fmt -check` en `terraform validate`.
# Graph-endpoints volgen learn.microsoft.com/entra/agent-id en de Graph v1.0-referentie
# (geraadpleegd 18-10-2026).
#
# Opbouw, gelijk aan de Keycloak-variant:
# - de agent heeft een eigen identiteit (agent identity uit een blueprint, met sponsor en eigenaar);
# - geen geheim: de blueprint authenticeert met een user-assigned managed identity als federated
#   identity credential;
# - de dossier-API publiceert alleen de scopes die de agent mag krijgen; `besluit.nemen` bestaat
#   wel voor medewerkers, maar wordt nooit aan de agent toegekend.

locals {
  graph = "https://graph.microsoft.com/v1.0"
}

resource "azapi_resource" "groep" {
  type      = "Microsoft.Resources/resourceGroups@2024-03-01"
  parent_id = "/subscriptions/${var.subscription_id}"
  name      = "rg-${var.naam}"
  location  = var.locatie
}

resource "azapi_resource" "identiteit" {
  type      = "Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30"
  parent_id = azapi_resource.groep.id
  name      = "id-${var.naam}"
  location  = var.locatie
  response_export_values = {
    principal_id = "properties.principalId"
    client_id    = "properties.clientId"
  }
}

# De beschermde dossier-API met gedelegeerde scopes.
resource "random_uuid" "scope" {
  for_each = toset(["dossier.lezen", "notitie.schrijven", "besluit.nemen"])
}

resource "msgraph_resource" "dossier_api" {
  url = "applications"
  body = {
    displayName    = "Dossier-API ${var.naam}"
    signInAudience = "AzureADMyOrg"
    api = {
      requestedAccessTokenVersion = 2
      oauth2PermissionScopes = [
        for scope, id in random_uuid.scope : {
          id                      = id.result
          value                   = scope
          type                    = "User"
          isEnabled               = true
          adminConsentDisplayName = scope
          adminConsentDescription = "Gedelegeerde toegang: ${scope}"
        }
      ]
    }
  }
  response_export_values = {
    app_id = "appId"
  }
}

resource "msgraph_update_resource" "dossier_api_uri" {
  url = "applications/${msgraph_resource.dossier_api.id}"
  body = {
    identifierUris = ["api://${msgraph_resource.dossier_api.output.app_id}"]
  }
}

resource "msgraph_resource" "dossier_api_principal" {
  url = "servicePrincipals"
  body = {
    appId = msgraph_resource.dossier_api.output.app_id
  }
}

# Blueprint: het sjabloon waaruit agent identities ontstaan, met sponsor en eigenaar.
resource "msgraph_resource" "blueprint" {
  url = "applications/microsoft.graph.agentIdentityBlueprint"
  body = {
    displayName           = "Blueprint ${var.naam}"
    "sponsors@odata.bind" = ["${local.graph}/users/${var.sponsor_object_id}"]
    "owners@odata.bind"   = ["${local.graph}/users/${var.eigenaar_object_id}"]
  }
  response_export_values = {
    app_id = "appId"
  }
}

# De werkplek vraagt voor de medewerker een token met de blueprint als audience (Tc); de agent
# wisselt dat in stap 3 in. Daarvoor publiceert de blueprint een identifier-URI en een scope.
resource "random_uuid" "toegang_agent" {}

resource "msgraph_update_resource" "blueprint_api" {
  url = "applications/${msgraph_resource.blueprint.id}"
  body = {
    identifierUris = ["api://${msgraph_resource.blueprint.output.app_id}"]
    api = {
      oauth2PermissionScopes = [
        {
          id                      = random_uuid.toegang_agent.result
          value                   = "access_agent"
          type                    = "User"
          isEnabled               = true
          adminConsentDisplayName = "Toegang tot de dossierassistent"
          adminConsentDescription = "De werkplek mag de agent aanroepen namens de aangemelde medewerker."
        }
      ]
    }
  }
}

# Geen geheim: de managed identity is de enige credential van de blueprint.
resource "msgraph_resource" "blueprint_fic" {
  url = "applications/${msgraph_resource.blueprint.id}/federatedIdentityCredentials"
  body = {
    name      = "managed-identity"
    issuer    = "https://login.microsoftonline.com/${var.tenant_id}/v2.0"
    subject   = azapi_resource.identiteit.output.principal_id
    audiences = ["api://AzureADTokenExchange"]
  }
}

resource "msgraph_resource" "blueprint_principal" {
  url = "servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal"
  body = {
    appId = msgraph_resource.blueprint.output.app_id
  }
}

# De agent zelf: een eigen identiteit per agent, met dezelfde sponsor.
resource "msgraph_resource" "agent" {
  url = "servicePrincipals/microsoft.graph.agentIdentity"
  body = {
    displayName              = "Dossierassistent ${var.naam}"
    agentIdentityBlueprintId = msgraph_resource.blueprint.output.app_id
    "sponsors@odata.bind"    = ["${local.graph}/users/${var.sponsor_object_id}"]
  }
  response_export_values = {
    app_id = "appId"
  }
  depends_on = [msgraph_resource.blueprint_principal]
}

# Beheerdersconsent voor precies twee scopes. `besluit.nemen` ontbreekt bewust.
resource "msgraph_resource" "toestemming" {
  url = "oauth2PermissionGrants"
  body = {
    clientId    = msgraph_resource.agent.id
    consentType = "AllPrincipals"
    resourceId  = msgraph_resource.dossier_api_principal.id
    scope       = "dossier.lezen notitie.schrijven"
  }
}
