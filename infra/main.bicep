targetScope = 'subscription'

@description('Environment name used by azd. Resource names are generated, not user inputs.')
@minLength(1)
@maxLength(32)
param environmentName string

@description('Region supporting both mandatory models. Start with eastus2 and run the deployment preflight.')
@metadata({
  azd: {
    type: 'location'
  }
})
param location string

@description('Single-tenant Entra SPA application ID, populated by the owner-auth initialization hook.')
@minLength(36)
@maxLength(36)
param clientAppId string

@description('Owner user object ID in the application tenant, never the deployment service principal.')
@minLength(36)
@maxLength(36)
param adminUserOid string

param tenantId string = tenant().tenantId

@description('Advanced application kill switch. Guest chat and voice are public unless this is true.')
param disablePublicAccess bool = false

@description('Internal azd package state: preserve the published image when reprovisioning.')
@minLength(1)
param backendImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

var resourceToken = uniqueString(subscription().id, environmentName, location)
var tags = {
  'azd-env-name': environmentName
  application: 'persona-assistant'
}

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module assistant 'resources.bicep' = {
  name: 'persona-assistant'
  scope: resourceGroup
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    tenantId: tenantId
    clientAppId: clientAppId
    adminUserOid: adminUserOid
    disablePublicAccess: disablePublicAccess
    backendImage: backendImage
  }
}

output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenantId
output AZURE_RESOURCE_GROUP string = resourceGroup.name
output AZURE_CLIENT_APP_ID string = clientAppId
output ADMIN_USER_OID string = adminUserOid
output BACKEND_URI string = assistant.outputs.backendUri
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = assistant.outputs.registryEndpoint
output AZURE_OPENAI_SERVICE string = assistant.outputs.openAiService
output AZURE_OPENAI_ENDPOINT string = assistant.outputs.openAiEndpoint
output AZURE_OPENAI_CHATGPT_MODEL string = assistant.outputs.textModel
output AZURE_OPENAI_CHATGPT_DEPLOYMENT string = assistant.outputs.textDeployment
output AZURE_OPENAI_REALTIME_DEPLOYMENT string = assistant.outputs.realtimeDeployment
output AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT string = assistant.outputs.transcriptionDeployment
output AZURE_OPENAI_REALTIME_VOICE string = assistant.outputs.realtimeVoice
output AZURE_COSMOSDB_ACCOUNT string = assistant.outputs.cosmosAccount
output AZURE_ADMIN_SETTINGS_DATABASE string = assistant.outputs.settingsDatabase
output AZURE_ADMIN_SETTINGS_CONTAINER string = assistant.outputs.settingsContainer
output AZURE_ANALYTICS_CONTAINER string = assistant.outputs.analyticsContainer
