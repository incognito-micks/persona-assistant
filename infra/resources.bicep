targetScope = 'resourceGroup'

param location string
param resourceToken string
param tags object
param tenantId string
param clientAppId string
param adminUserOid string
param disablePublicAccess bool
param backendImage string

// Stable realtime model contract: https://learn.microsoft.com/azure/foundry/openai/concepts/realtime-2
// Availability and quota are subscription/region-specific; the preprovision hook checks both models.
var models = loadJsonContent('deployment-models.json')
var text = models.text
var realtime = models.realtime
var transcription = models.transcription
var backendName = 'ca-persona-${resourceToken}'
var cosmosAccountName = 'cosmos-persona-${resourceToken}'
var databaseName = 'persona-assistant'
var settingsContainerName = 'admin_settings'
var analyticsContainerName = 'analytics'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-persona-${resourceToken}'
  location: location
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-04-01' = {
  name: 'acrpersona${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    policies: {
      azureADAuthenticationAsArmPolicy: {
        status: 'enabled'
      }
    }
  }
}

resource registryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, 'AcrPull')
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource containerEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: 'cae-persona-${resourceToken}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: null
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

// Text, voice and input transcription are part of the supported default experience.
module openAi 'br/public:avm/res/cognitive-services/account:0.8.0' = {
  name: 'openai'
  params: {
    name: 'oai-persona-${resourceToken}'
    location: location
    tags: tags
    enableTelemetry: false
    kind: 'OpenAI'
    sku: 'S0'
    customSubDomainName: 'oai-persona-${resourceToken}'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
    }
    deployments: [
      {
        name: text.deployment
        model: {
          format: 'OpenAI'
          name: text.name
          version: text.version
        }
        sku: {
          name: text.sku
          capacity: text.capacity
        }
      }
      {
        name: realtime.deployment
        model: {
          format: 'OpenAI'
          name: realtime.name
          version: realtime.version
        }
        sku: {
          name: realtime.sku
          capacity: realtime.capacity
        }
      }
      {
        name: transcription.deployment
        model: {
          format: 'OpenAI'
          name: transcription.name
          version: transcription.version
        }
        sku: {
          name: transcription.sku
          capacity: transcription.capacity
        }
      }
    ]
    roleAssignments: [
      {
        // Cognitive Services OpenAI User, scoped to this account.
        roleDefinitionIdOrName: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
        principalId: identity.properties.principalId
        principalType: 'ServicePrincipal'
      }
    ]
  }
}

// Settings, shared admission limits and analytics do not depend on login or history.
module cosmosDb 'br/public:avm/res/document-db/database-account:0.6.1' = {
  name: 'cosmosdb'
  params: {
    name: cosmosAccountName
    location: location
    tags: tags
    enableTelemetry: false
    disableLocalAuth: true
    capabilitiesToAdd: [
      'EnableServerless'
    ]
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    networkRestrictions: {
      ipRules: []
      networkAclBypass: 'None'
      publicNetworkAccess: 'Enabled'
      virtualNetworkRules: []
    }
    sqlDatabases: [
      {
        name: databaseName
        containers: [
          {
            name: settingsContainerName
            kind: 'Hash'
            paths: [
              '/id'
            ]
            // Only records with their own ttl expire; durable settings remain.
            defaultTtl: -1
          }
          {
            name: analyticsContainerName
            kind: 'Hash'
            paths: [
              '/user_oid'
            ]
          }
        ]
      }
    ]
  }
}

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = {
  name: cosmosAccountName
}

resource cosmosDataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, identity.id, 'data-contributor')
  properties: {
    principalId: identity.properties.principalId
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: cosmosAccount.id
  }
  dependsOn: [
    cosmosDb
  ]
}

var appEnvVariables = {
  RUNNING_IN_PRODUCTION: 'true'
  DISABLE_PUBLIC_ACCESS: disablePublicAccess ? 'true' : 'false'
  AZURE_CLIENT_ID: identity.properties.clientId
  AZURE_TENANT_ID: tenantId
  AZURE_CLIENT_APP_ID: clientAppId
  ADMIN_USER_OID: adminUserOid
  OPENAI_HOST: 'azure'
  AZURE_OPENAI_SERVICE: openAi.outputs.name
  AZURE_OPENAI_CHATGPT_MODEL: text.name
  AZURE_OPENAI_CHATGPT_DEPLOYMENT: text.deployment
  AZURE_OPENAI_CHATGPT_CONTEXT_WINDOW: '16384'
  USE_REALTIME_VOICE: 'true'
  AZURE_OPENAI_REALTIME_DEPLOYMENT: realtime.deployment
  AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT: transcription.deployment
  AZURE_OPENAI_REALTIME_VOICE: models.voice
  USE_CHAT_HISTORY_BROWSER: 'true'
  USE_CHAT_HISTORY_COSMOS: 'false'
  AZURE_COSMOSDB_ACCOUNT: cosmosDb.outputs.name
  AZURE_ADMIN_SETTINGS_DATABASE: databaseName
  AZURE_ADMIN_SETTINGS_CONTAINER: settingsContainerName
  AZURE_ANALYTICS_CONTAINER: analyticsContainerName
}

resource backend 'Microsoft.App/containerApps@2025-01-01' = {
  name: backendName
  location: location
  tags: union(tags, {
    'azd-service-name': 'backend'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  dependsOn: [
    registryPull
    cosmosDataContributor
  ]
  properties: {
    managedEnvironmentId: containerEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'backend'
          // Package state avoids referencing this same resource during an update.
          image: backendImage
          env: map(items(appEnvVariables), setting => {
            name: setting.key
            value: setting.value
          })
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 3
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '10'
              }
            }
          }
        ]
      }
    }
  }
}

output backendUri string = 'https://${backend.properties.configuration.ingress.fqdn}'
output registryEndpoint string = registry.properties.loginServer
output openAiService string = openAi.outputs.name
output openAiEndpoint string = openAi.outputs.endpoint
output textModel string = text.name
output textDeployment string = text.deployment
output realtimeDeployment string = realtime.deployment
output transcriptionDeployment string = transcription.deployment
output realtimeVoice string = models.voice
output cosmosAccount string = cosmosDb.outputs.name
output settingsDatabase string = databaseName
output settingsContainer string = settingsContainerName
output analyticsContainer string = analyticsContainerName
