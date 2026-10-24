import { createContext, useContext } from "react";
import { PublicStatus } from "./api/models";

export interface AppContextType {
    publicStatus: PublicStatus | null;
    refreshPublicStatus: () => Promise<void>;
}

export const AppContext = createContext<AppContextType>({
    publicStatus: null,
    refreshPublicStatus: async () => {}
});

export const useAppContext = () => useContext(AppContext);
