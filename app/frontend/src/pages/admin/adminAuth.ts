import { InteractionRequiredAuthError, PublicClientApplication, type Configuration } from "@azure/msal-browser";

export interface OwnerAuthClient {
    getCachedToken: () => Promise<string | undefined>;
    getToken: () => Promise<string | undefined>;
    signIn: () => Promise<string>;
    signOut: () => Promise<void>;
}

export default async function createOwnerAuth(config: Configuration): Promise<OwnerAuthClient> {
    const instance = new PublicClientApplication(config);
    await instance.initialize();
    const cachedAccount = instance.getAllAccounts()[0];
    if (cachedAccount) instance.setActiveAccount(cachedAccount);
    const scopes = ["openid", "profile", "email"];

    const getToken = async (allowPopup: boolean): Promise<string | undefined> => {
        const account = instance.getActiveAccount();
        if (!account) return undefined;
        try {
            return (await instance.acquireTokenSilent({ scopes, account })).idToken;
        } catch (failure) {
            if (!(failure instanceof InteractionRequiredAuthError)) throw failure;
            if (!allowPopup) return undefined;
            return (await instance.acquireTokenPopup({ scopes, account })).idToken;
        }
    };

    return {
        getCachedToken: () => getToken(false),
        getToken: () => getToken(true),
        signIn: async () => {
            const result = await instance.loginPopup({ scopes });
            if (result.account) instance.setActiveAccount(result.account);
            return result.idToken;
        },
        signOut: async () => {
            await instance.logoutPopup({
                account: instance.getActiveAccount(),
                mainWindowRedirectUri: `${window.location.origin}/#/admin`
            });
        }
    };
}
