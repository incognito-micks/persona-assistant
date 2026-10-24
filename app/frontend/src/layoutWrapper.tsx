import { useCallback, useEffect, useState } from "react";
import { AppContext } from "./appContext";
import { getPublicStatus } from "./api";
import { PublicStatus } from "./api/models";
import Layout from "./pages/layout/Layout";

const LayoutWrapper = () => {
    const [publicStatus, setPublicStatus] = useState<PublicStatus | null>(null);
    const refreshPublicStatus = useCallback(async () => {
        setPublicStatus(await getPublicStatus());
    }, []);

    useEffect(() => {
        const controller = new AbortController();
        getPublicStatus(controller.signal)
            .then(status => {
                if (!controller.signal.aborted) setPublicStatus(status);
            })
            .catch(error => {
                if (!controller.signal.aborted) console.error("Failed to fetch public status", error);
            });
        return () => controller.abort();
    }, []);

    return (
        <AppContext.Provider value={{ publicStatus, refreshPublicStatus }}>
            <Layout />
        </AppContext.Provider>
    );
};

export default LayoutWrapper;
