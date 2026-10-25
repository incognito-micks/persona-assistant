import { createContext, useContext } from "react";
import type { Config, PublicStatus } from "./api/models";

export interface AppContextType {
    publicStatus: PublicStatus | null;
    refreshPublicStatus: () => Promise<void>;
    config: Config | null;
    configError: boolean;
    retryConfig: () => void;
}

export const AppContext = createContext<AppContextType>({
    publicStatus: null,
    refreshPublicStatus: async () => {},
    config: null,
    configError: false,
    retryConfig: () => {}
});

export const useAppContext = () => useContext(AppContext);
