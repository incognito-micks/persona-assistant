import type { PublicStatus } from "./api/models";

export const getAssistantName = (profile: PublicStatus | null): string => profile?.assistant_name?.trim() || "Persona Assistant";

export const getAvatarUrl = (profile: PublicStatus | null): string => profile?.avatar_url?.trim() || "/avatar.svg";

export const isSafeProfileImageUrl = (value: string): boolean => {
    if (!value) return true;
    if (/[\u0000-\u0020\u007f\\]/.test(value)) return false;
    if (value.startsWith("/") && !value.startsWith("//")) return !value.split(/[?#]/)[0].split("/").includes("..");
    try {
        const url = new URL(value);
        return url.protocol === "https:" && !!url.hostname && !url.username && !url.password;
    } catch (failure) {
        if (failure instanceof TypeError) return false;
        throw failure;
    }
};
