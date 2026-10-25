import { useEffect, useMemo, useRef, useState } from "react";
import { Dialog, DialogFooter, DialogType, Panel, PanelType, Spinner } from "@fluentui/react";
import { Add20Regular, ChevronLeft20Regular, Dismiss20Regular } from "@fluentui/react-icons";
import { useTranslation } from "react-i18next";
import { HistoryItem } from "../HistoryItem";
import { useHistoryManager } from "../HistoryProviders";
import type { Answers, HistoryMetaData, HistoryProviderOptions } from "../HistoryProviders/IProvider";
import styles from "./ChatSidebar.module.css";

const HISTORY_COUNT_PER_LOAD = 20;

interface Props {
    provider: HistoryProviderOptions;
    isMobile: boolean;
    conversationBusy: boolean;
    activeId: string | null;
    latestSavedChat: HistoryMetaData | null;
    onClose: () => void;
    onNewChat: () => void;
    onChatSelected: (id: string, answers: Answers) => void;
    onChatDeleted: (id: string) => void;
    onBusyChange: (busy: boolean) => void;
}

export const ChatSidebar = ({
    provider,
    isMobile,
    conversationBusy,
    activeId,
    latestSavedChat,
    onClose,
    onNewChat,
    onChatSelected,
    onChatDeleted,
    onBusyChange
}: Props) => {
    const { t } = useTranslation();
    const historyManager = useHistoryManager(provider);
    const [history, setHistory] = useState<HistoryMetaData[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    const [isDeleting, setIsDeleting] = useState(false);
    const [hasMore, setHasMore] = useState(false);
    const [error, setError] = useState<string>();
    const [pendingDelete, setPendingDelete] = useState<HistoryMetaData>();
    const requestVersion = useRef(0);
    const isBusy = useRef(false);
    const lastSeenSave = useRef(latestSavedChat);
    const newChatButton = useRef<HTMLButtonElement>(null);

    const runHistoryAction = async <Result,>(action: () => Promise<Result>, onSuccess: (result: Result) => void, blockConversation = false) => {
        if (isBusy.current) return;
        const version = requestVersion.current;
        isBusy.current = true;
        setIsLoading(true);
        setError(undefined);
        if (blockConversation) onBusyChange(true);
        try {
            const result = await action();
            if (version === requestVersion.current) onSuccess(result);
        } catch (failure) {
            if (version === requestVersion.current) {
                console.error("Chat history action failed:", failure);
                setError(t("history.actionError"));
            }
        } finally {
            if (version === requestVersion.current) {
                isBusy.current = false;
                setIsLoading(false);
                if (blockConversation) onBusyChange(false);
            }
        }
    };

    const loadMore = () =>
        runHistoryAction(
            () => historyManager.getNextItems(HISTORY_COUNT_PER_LOAD),
            items => {
                setHasMore(items.length > 0);
                setHistory(previous => mergeHistory(previous, items));
            }
        );

    const reloadHistory = () => {
        if (isBusy.current) return;
        historyManager.resetContinuationToken();
        setHistory([]);
        setHasMore(false);
        void loadMore();
    };

    useEffect(() => {
        reloadHistory();
        return () => {
            requestVersion.current += 1;
            isBusy.current = false;
            onBusyChange(false);
        };
    }, [historyManager, onBusyChange]);

    useEffect(() => {
        if (latestSavedChat !== lastSeenSave.current) {
            lastSeenSave.current = latestSavedChat;
            if (latestSavedChat) setHistory(previous => mergeHistory(previous, [latestSavedChat]));
        }
    }, [latestSavedChat]);

    const selectChat = (id: string) => {
        if (conversationBusy) return;
        void runHistoryAction(
            () => historyManager.getItem(id),
            answers => {
                if (!answers?.length) {
                    console.error("Saved conversation is missing or empty:", id);
                    setError(t("history.notFound"));
                    return;
                }
                onChatSelected(id, answers);
            },
            true
        );
    };

    const confirmDelete = () => {
        if (!pendingDelete || conversationBusy || isBusy.current) return;
        const id = pendingDelete.id;
        const version = requestVersion.current;
        setIsDeleting(true);
        setPendingDelete(undefined);
        void runHistoryAction(
            () => historyManager.deleteItem(id),
            () => {
                setHistory(previous => previous.filter(item => item.id !== id));
                onChatDeleted(id);
                newChatButton.current?.focus();
            },
            true
        ).finally(() => {
            if (version === requestVersion.current) setIsDeleting(false);
        });
    };

    const groupedHistory = useMemo(() => groupHistory(history), [history]);
    const close = () => {
        if (!isDeleting && !pendingDelete) onClose();
    };
    const closeButton = (
        <button type="button" className={styles.iconButton} onClick={close} disabled={isDeleting} aria-label={t("history.closeChatHistory")}>
            {isMobile ? <Dismiss20Regular aria-hidden /> : <ChevronLeft20Regular aria-hidden />}
        </button>
    );
    const content = (
        <div className={styles.sidebarContent}>
            <div className={styles.actions}>
                <button ref={newChatButton} type="button" data-new-chat="true" className={styles.newChatButton} onClick={onNewChat}>
                    <Add20Regular aria-hidden />
                    {t("history.newChat")}
                </button>
                {!isMobile && closeButton}
            </div>
            {!isMobile && <h2 className={styles.heading}>{t("history.chatHistory")}</h2>}
            <div className={styles.historyList} aria-busy={isLoading}>
                {error && (
                    <div role="alert" className={styles.error}>
                        <p>{error}</p>
                        <button type="button" className={styles.secondaryButton} onClick={reloadHistory}>
                            {t("history.reload")}
                        </button>
                    </div>
                )}
                {Object.entries(groupedHistory).map(([group, items]) => (
                    <section key={group} className={styles.group}>
                        <h3 className={styles.groupLabel}>{t(group)}</h3>
                        {items.map(item => (
                            <HistoryItem
                                key={item.id}
                                item={item}
                                isActive={item.id === activeId}
                                disabled={isLoading || conversationBusy}
                                onSelect={selectChat}
                                onDelete={id => setPendingDelete(history.find(entry => entry.id === id))}
                            />
                        ))}
                    </section>
                ))}
                {isLoading && <Spinner label={t("history.loading")} className={styles.loading} />}
                {!history.length && !isLoading && !error && <p className={styles.empty}>{t("history.noHistory")}</p>}
                {hasMore && !isLoading && !error && (
                    <button type="button" className={styles.secondaryButton} onClick={loadMore}>
                        {t("history.loadMore")}
                    </button>
                )}
            </div>
            <p className={styles.storageNotice}>{t("privacy.localHistory")}</p>
            <Dialog
                hidden={!pendingDelete}
                onDismiss={() => setPendingDelete(undefined)}
                dialogContentProps={{
                    type: DialogType.normal,
                    title: t("history.deleteModalTitle"),
                    subText: t("history.deleteModalDescription"),
                    showCloseButton: false
                }}
                modalProps={{ isBlocking: false, className: styles.deleteDialog }}
            >
                <DialogFooter>
                    <button type="button" className={styles.secondaryButton} onClick={() => setPendingDelete(undefined)}>
                        {t("history.cancelLabel")}
                    </button>
                    <button type="button" className={styles.confirmDeleteButton} onClick={confirmDelete}>
                        {t("history.deleteLabel")}
                    </button>
                </DialogFooter>
            </Dialog>
        </div>
    );

    return isMobile ? (
        <Panel
            isOpen
            type={PanelType.customNear}
            customWidth="min(320px, calc(100vw - 48px))"
            headerText={t("history.chatHistory")}
            headerTextProps={{ "aria-level": 2 }}
            isBlocking
            onDismiss={close}
            className={styles.drawer}
            onRenderNavigationContent={() => closeButton}
            focusTrapZoneProps={{ firstFocusableTarget: "[data-new-chat='true']", enableAriaHiddenSiblings: true }}
            overlayProps={{ styles: { root: { backgroundColor: "var(--app-overlay)", opacity: 1 } } }}
            styles={{
                main: { overflowY: "hidden" },
                commands: { backgroundColor: "var(--app-surface)" },
                navigation: { paddingInlineEnd: "0.5rem" },
                contentInner: { minHeight: 0 },
                content: { padding: 0, minHeight: 0, display: "flex", flex: 1 },
                scrollableContent: { display: "flex", flexDirection: "column", flex: 1, minHeight: 0, overflow: "hidden" },
                headerText: { color: "var(--app-text)" }
            }}
        >
            {content}
        </Panel>
    ) : (
        <aside className={styles.sidebar} aria-label={t("history.chatHistory")}>
            {content}
        </aside>
    );
};

function mergeHistory(previous: HistoryMetaData[], incoming: HistoryMetaData[]): HistoryMetaData[] {
    const items = new Map(previous.map(item => [item.id, item]));
    for (const item of incoming) {
        const existing = items.get(item.id);
        if (!existing || item.timestamp >= existing.timestamp) items.set(item.id, item);
    }
    return [...items.values()].sort((left, right) => right.timestamp - left.timestamp);
}

function groupHistory(history: HistoryMetaData[]): Record<string, HistoryMetaData[]> {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    const lastWeek = new Date(today);
    lastWeek.setDate(lastWeek.getDate() - 7);
    const lastMonth = new Date(today);
    lastMonth.setDate(lastMonth.getDate() - 30);
    const groups: Record<string, HistoryMetaData[]> = {};
    for (const item of history) {
        const date = new Date(item.timestamp);
        const group =
            date >= today
                ? "history.today"
                : date >= yesterday
                  ? "history.yesterday"
                  : date >= lastWeek
                    ? "history.last7days"
                    : date >= lastMonth
                      ? "history.last30days"
                      : date.toLocaleDateString(undefined, { year: "numeric", month: "long" });
        (groups[group] ??= []).push(item);
    }
    return groups;
}
