import { InteractionRequiredAuthError, PublicClientApplication, type Configuration } from "@azure/msal-browser";

export interface OwnerAuthClient {
    getCachedToken: () => Promise<string | undefined>;
    getToken: () => Promise<string | undefined>;
    signIn: () => Promise<string>;
    signOut: () => Promise<void>;
    resetSignIn: () => Promise<void>;
    getAccount: () => { name?: string; username: string } | undefined;
}

export default async function createOwnerAuth(config: Configuration): Promise<OwnerAuthClient> {
    const instance = new PublicClientApplication(config);
    await instance.initialize();
    const cachedAccount = instance.getAllAccounts()[0];
    if (cachedAccount) instance.setActiveAccount(cachedAccount);
    const scopes = ["openid", "profile", "email"];

    const signIn = async (): Promise<string> => {
        const result = await instance.loginPopup({ scopes, prompt: "select_account" });
        if (result.account) instance.setActiveAccount(result.account);
        if (!result.idToken) throw new Error("Microsoft sign-in did not return an ID token");
        return result.idToken;
    };

    const getToken = async (allowPopup: boolean): Promise<string | undefined> => {
        const account = instance.getActiveAccount();
        if (!account) return allowPopup ? signIn() : undefined;
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
        signIn,
        resetSignIn: () => instance.clearCache(),
        getAccount: () => {
            const account = instance.getActiveAccount();
            return account ? { name: account.name, username: account.username } : undefined;
        },
        signOut: async () => {
            await instance.logoutPopup({
                account: instance.getActiveAccount(),
                mainWindowRedirectUri: `${window.location.origin}/#/admin`
            });
        }
    };
}
