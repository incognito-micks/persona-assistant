import { DefaultButton, MessageBar, MessageBarType, Panel, PanelType, Spinner } from "@fluentui/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { HistoryData, HistoryItem } from "../HistoryItem";
import { Answers, HistoryProviderOptions } from "../HistoryProviders/IProvider";
import { useHistoryManager, HistoryMetaData } from "../HistoryProviders";
import { useTranslation } from "react-i18next";
import styles from "./HistoryPanel.module.css";

const HISTORY_COUNT_PER_LOAD = 20;

export const HistoryPanel = ({
    provider,
    isOpen,
    onClose,
    onChatSelected
}: {
    provider: HistoryProviderOptions;
    isOpen: boolean;
    onClose: () => void;
    onChatSelected: (answers: Answers) => void;
}) => {
    const historyManager = useHistoryManager(provider);
    const [history, setHistory] = useState<HistoryMetaData[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    const [hasMoreHistory, setHasMoreHistory] = useState(false);
    const [error, setError] = useState<string>();
    const requestVersion = useRef(0);
    const isBusy = useRef(false);

    useEffect(() => {
        if (!isOpen) return;
        setHistory([]);
        historyManager.resetContinuationToken();
        setHasMoreHistory(true);
        void loadMoreHistory();
        return () => {
            requestVersion.current += 1;
            isBusy.current = false;
        };
    }, [isOpen, historyManager]);

    const runHistoryAction = async <Result,>(action: (token?: string) => Promise<Result>, onSuccess: (result: Result) => void) => {
        if (isBusy.current || !isOpen) return;
        const version = requestVersion.current;
        isBusy.current = true;
        setIsLoading(true);
        setError(undefined);
        try {
            const result = await action();
            if (version === requestVersion.current) onSuccess(result);
        } catch (requestError) {
            if (version === requestVersion.current) {
                setError(requestError instanceof Error ? requestError.message : "Unable to load chat history.");
            }
        } finally {
            if (version === requestVersion.current) {
                isBusy.current = false;
                setIsLoading(false);
            }
        }
    };

    const loadMoreHistory = () =>
        runHistoryAction(
            token => historyManager.getNextItems(HISTORY_COUNT_PER_LOAD, token),
            items => {
                setHasMoreHistory(items.length > 0);
                setHistory(previousHistory => [...previousHistory, ...items]);
            }
        );

    const handleSelect = (id: string) =>
        runHistoryAction(
            token => historyManager.getItem(id, token),
            item => {
                if (item) onChatSelected(item);
            }
        );

    const handleDelete = (id: string) =>
        runHistoryAction(
            token => historyManager.deleteItem(id, token),
            () => {
                setHistory(previousHistory => previousHistory.filter(item => item.id !== id));
            }
        );

    const groupedHistory = useMemo(() => groupHistory(history), [history]);

    const { t } = useTranslation();

    return (
        <Panel
            type={PanelType.customNear}
            style={{ padding: "0px" }}
            headerText={t("history.chatHistory")}
            closeButtonAriaLabel={t("history.closeChatHistory", "Close chat history")}
            customWidth="300px"
            isBlocking={false}
            isOpen={isOpen}
            onDismiss={onClose}
        >
            <div>
                {error && <MessageBar messageBarType={MessageBarType.error}>{error}</MessageBar>}
                {Object.entries(groupedHistory).map(([group, items]) => (
                    <div key={group} className={styles.group}>
                        <p className={styles.groupLabel}>{t(group)}</p>
                        {items.map(item => (
                            <HistoryItem key={item.id} item={item} disabled={isLoading} onSelect={handleSelect} onDelete={handleDelete} />
                        ))}
                    </div>
                ))}
                {isLoading && <Spinner style={{ marginTop: "10px" }} />}
                {history.length === 0 && !isLoading && !error && <p>{t("history.noHistory")}</p>}
                {hasMoreHistory && !isLoading && (
                    <DefaultButton iconProps={{ iconName: "ChevronDown" }} onClick={loadMoreHistory}>
                        {t("history.loadMore", "Load more")}
                    </DefaultButton>
                )}
            </div>
        </Panel>
    );
};

function groupHistory(history: HistoryData[]) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    const lastWeek = new Date(today);
    lastWeek.setDate(lastWeek.getDate() - 7);
    const lastMonth = new Date(today);
    lastMonth.setDate(lastMonth.getDate() - 30);

    return history.reduce(
        (groups, item) => {
            const itemDate = new Date(item.timestamp);
            let group;

            if (itemDate >= today) {
                group = "history.today";
            } else if (itemDate >= yesterday) {
                group = "history.yesterday";
            } else if (itemDate >= lastWeek) {
                group = "history.last7days";
            } else if (itemDate >= lastMonth) {
                group = "history.last30days";
            } else {
                group = itemDate.toLocaleDateString(undefined, { year: "numeric", month: "long" });
            }

            if (!groups[group]) {
                groups[group] = [];
            }
            groups[group].push(item);
            return groups;
        },
        {} as Record<string, HistoryData[]>
    );
}
