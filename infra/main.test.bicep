targetScope = 'subscription'

// Static-analysis inputs only; this file never provisions resources during validation.
module main 'main.bicep' = {
  name: 'persona-assistant-test'
  params: {
    environmentName: 'validation'
    location: 'eastus2'
    clientAppId: '00000000-0000-0000-0000-000000000001'
    adminUserOid: '00000000-0000-0000-0000-000000000002'
  }
}
