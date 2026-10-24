import type { PublicStatus } from "./api/models";

export const getAssistantName = (profile: PublicStatus | null): string => profile?.assistant_name?.trim() || "Persona Assistant";

export const getAvatarUrl = (profile: PublicStatus | null): string => profile?.avatar_url?.trim() || "/avatar.svg";
