import { useCallback, useEffect, useState } from "react";
import { AppContext } from "./appContext";
import { configApi, getPublicStatus } from "./api";
import type { Config, PublicStatus } from "./api/models";
import Layout from "./pages/layout/Layout";

const LayoutWrapper = () => {
    const [publicStatus, setPublicStatus] = useState<PublicStatus | null>(null);
    const [config, setConfig] = useState<Config | null>(null);
    const [configError, setConfigError] = useState(false);
    const [configAttempt, setConfigAttempt] = useState(0);
    const retryConfig = useCallback(() => setConfigAttempt(attempt => attempt + 1), []);
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

    useEffect(() => {
        const controller = new AbortController();
        setConfigError(false);
        configApi(controller.signal)
            .then(value => {
                if (!controller.signal.aborted) setConfig(value);
            })
            .catch(error => {
                if (!controller.signal.aborted) {
                    console.error("Failed to fetch assistant capabilities", error);
                    setConfigError(true);
                }
            });
        return () => controller.abort();
    }, [configAttempt]);

    return (
        <AppContext.Provider value={{ publicStatus, refreshPublicStatus, config, configError, retryConfig }}>
            <Layout />
        </AppContext.Provider>
    );
};

export default LayoutWrapper;
