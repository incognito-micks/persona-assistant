const BACKEND_URI = "";

import { ChatAppRequest, Config, HistoryListApiResponse, HistoryApiResponse, AdminSettings, PublicStatus, AnalyticsSummary } from "./models";
export async function getHeaders(idToken: string | undefined): Promise<Record<string, string>> {
    if (idToken) {
        return { Authorization: `Bearer ${idToken}` };
    }

    return {};
}

export async function configApi(signal?: AbortSignal): Promise<Config> {
    const response = await fetch(`${BACKEND_URI}/config`, {
        method: "GET",
        credentials: "omit",
        signal
    });

    if (!response.ok) {
        throw new Error(`Loading configuration failed: ${response.status}`);
    }

    return (await response.json()) as Config;
}

export async function chatApi(request: ChatAppRequest, shouldStream: boolean, signal?: AbortSignal): Promise<Response> {
    let url = `${BACKEND_URI}/chat`;
    if (shouldStream) {
        url += "/stream";
    }
    return await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
        credentials: "omit",
        signal
    });
}

export async function postChatHistoryApi(item: any, idToken: string): Promise<any> {
    const headers = await getHeaders(idToken);
    const response = await fetch("/chat_history", {
        method: "POST",
        headers: { ...headers, "Content-Type": "application/json" },
        body: JSON.stringify(item)
    });

    if (!response.ok) {
        throw new Error(`Posting chat history failed: ${response.statusText}`);
    }

    const dataResponse: any = await response.json();
    return dataResponse;
}

export async function getChatHistoryListApi(count: number, continuationToken: string | undefined, idToken: string): Promise<HistoryListApiResponse> {
    const headers = await getHeaders(idToken);
    const params = new URLSearchParams({ count: String(count) });
    if (continuationToken) {
        params.set("continuation_token", continuationToken);
    }

    const response = await fetch(`${BACKEND_URI}/chat_history/sessions?${params.toString()}`, {
        method: "GET",
        headers: { ...headers, "Content-Type": "application/json" }
    });

    if (!response.ok) {
        throw new Error(`Getting chat histories failed: ${response.statusText}`);
    }

    const dataResponse: HistoryListApiResponse = await response.json();
    return dataResponse;
}

export async function getChatHistoryApi(id: string, idToken: string): Promise<HistoryApiResponse> {
    const headers = await getHeaders(idToken);
    const response = await fetch(`/chat_history/sessions/${id}`, {
        method: "GET",
        headers: { ...headers, "Content-Type": "application/json" }
    });

    if (!response.ok) {
        throw new Error(`Getting chat history failed: ${response.statusText}`);
    }

    const dataResponse: HistoryApiResponse = await response.json();
    return dataResponse;
}

export async function deleteChatHistoryApi(id: string, idToken: string): Promise<any> {
    const headers = await getHeaders(idToken);
    const response = await fetch(`/chat_history/sessions/${id}`, {
        method: "DELETE",
        headers: { ...headers, "Content-Type": "application/json" }
    });

    if (!response.ok) {
        throw new Error(`Deleting chat history failed: ${response.statusText}`);
    }
}

export class AdminApiError extends Error {
    constructor(public readonly status: number) {
        super(`Owner administration request failed: ${status}`);
    }
}

let adminSettingsEtag: string | null = null;

export async function getAdminSettings(idToken: string): Promise<AdminSettings> {
    const headers = await getHeaders(idToken);
    const response = await fetch(`${BACKEND_URI}/api/admin/settings`, {
        method: "GET",
        headers: { ...headers, "Content-Type": "application/json" }
    });

    if (!response.ok) {
        throw new AdminApiError(response.status);
    }

    adminSettingsEtag = response.headers.get("ETag");
    return (await response.json()) as AdminSettings;
}

export async function updateAdminSettings(settings: Partial<AdminSettings>, idToken: string): Promise<AdminSettings> {
    const headers = await getHeaders(idToken);
    const response = await fetch(`${BACKEND_URI}/api/admin/settings`, {
        method: "PUT",
        headers: { ...headers, ...(adminSettingsEtag ? { "If-Match": adminSettingsEtag } : {}), "Content-Type": "application/json" },
        body: JSON.stringify(settings)
    });

    if (!response.ok) {
        throw new AdminApiError(response.status);
    }

    adminSettingsEtag = response.headers.get("ETag");
    return (await response.json()) as AdminSettings;
}

export async function getPublicStatus(signal?: AbortSignal): Promise<PublicStatus> {
    const response = await fetch(`${BACKEND_URI}/api/status`, {
        method: "GET",
        credentials: "omit",
        signal
    });

    if (!response.ok) {
        throw new Error(`Getting public status failed: ${response.statusText}`);
    }

    return (await response.json()) as PublicStatus;
}

export async function getAdminAnalytics(idToken: string, signal?: AbortSignal): Promise<AnalyticsSummary> {
    const headers = await getHeaders(idToken);
    const response = await fetch(`${BACKEND_URI}/api/admin/analytics`, {
        method: "GET",
        headers: { ...headers, "Content-Type": "application/json" },
        signal
    });

    if (!response.ok) {
        throw new AdminApiError(response.status);
    }

    return (await response.json()) as AnalyticsSummary;
}
