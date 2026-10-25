import { useRef, useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { Spinner } from "@fluentui/react";
import { Add20Regular, Navigation20Regular } from "@fluentui/react-icons";
import readNDJSONStream from "ndjson-readablestream";

import styles from "./Chat.module.css";

import { chatApi, ChatAppResponse, ChatAppResponseOrError, ChatAppRequest, ResponseMessage } from "../../api";
import { Answer, AnswerError, AnswerLoading } from "../../components/Answer";
import { QuestionInput } from "../../components/QuestionInput";
import { ExampleList } from "../../components/Example";
import { UserChatMessage } from "../../components/UserChatMessage";
import { ChatSidebar } from "../../components/ChatSidebar";
import { HistoryProviderOptions, useHistoryManager } from "../../components/HistoryProviders";
import { getHistoryTitle } from "../../components/HistoryProviders/IProvider";
import type { HistoryMetaData } from "../../components/HistoryProviders/IProvider";
import { useAppContext } from "../../appContext";
import { getAssistantName, getAvatarUrl } from "../../profile";
import { LanguagePicker } from "../../i18n/LanguagePicker";

const suggestFollowupQuestions = false;
const DESKTOP_QUERY = "(min-width: 992px)";

const Chat = () => {
    const { t, i18n } = useTranslation();
    const { publicStatus, config, configError, retryConfig } = useAppContext();
    const lastQuestionRef = useRef("");
    const conversationScroll = useRef<HTMLDivElement>(null);
    const activeRequest = useRef<AbortController | null>(null);
    const conversationVersion = useRef(0);
    const sidebarToggle = useRef<HTMLButtonElement>(null);
    const focusToggleAfterClose = useRef(false);

    const [isLoading, setIsLoading] = useState(false);
    const [isStreaming, setIsStreaming] = useState(false);
    const [isRequestActive, setIsRequestActive] = useState(false);
    const [error, setError] = useState<unknown>();
    const [answers, setAnswers] = useState<[user: string, response: ChatAppResponse][]>([]);
    const [streamedAnswers, setStreamedAnswers] = useState<[user: string, response: ChatAppResponse][]>([]);
    const [isNarrow, setIsNarrow] = useState(() => !window.matchMedia(DESKTOP_QUERY).matches);
    const [desktopSidebarExpanded, setDesktopSidebarExpanded] = useState(false);
    const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
    const [historyActionBusy, setHistoryActionBusy] = useState(false);
    const [activeHistoryId, setActiveHistoryId] = useState<string | null>(null);
    const [latestSavedChat, setLatestSavedChat] = useState<HistoryMetaData | null>(null);
    const [historyWarning, setHistoryWarning] = useState<string>();
    const [composerVersion, setComposerVersion] = useState(0);
    const [focusComposer, setFocusComposer] = useState(false);

    const historyProvider = config?.showChatHistoryBrowser ? HistoryProviderOptions.IndexedDB : HistoryProviderOptions.None;
    const historyManager = useHistoryManager(historyProvider);

    const handleAsyncRequest = async (
        question: string,
        previousAnswers: [string, ChatAppResponse][],
        responseBody: ReadableStream<Uint8Array>,
        signal: AbortSignal
    ) => {
        let answer = "";
        let askResponse: ChatAppResponse = {} as ChatAppResponse;
        const updateState = async (newContent: string) => {
            await new Promise(resolve => setTimeout(resolve, 33));
            signal.throwIfAborted();
            answer += newContent;
            setStreamedAnswers([
                ...previousAnswers,
                [question, { ...askResponse, message: { content: answer, role: askResponse.message?.role ?? "assistant" } }]
            ]);
        };
        try {
            signal.throwIfAborted();
            setStreamedAnswers(previousAnswers);
            setIsStreaming(true);
            for await (const event of readNDJSONStream(responseBody)) {
                signal.throwIfAborted();
                if (event["context"] && event["context"]["data_points"]) {
                    event["message"] = event["delta"];
                    askResponse = event as ChatAppResponse;
                } else if (event["delta"] && event["delta"]["content"]) {
                    setIsLoading(false);
                    await updateState(event["delta"]["content"]);
                } else if (event["context"]) {
                    askResponse.context = { ...askResponse.context, ...event["context"] };
                } else if (event["error"]) {
                    throw Error(event["error"]);
                }
            }
        } finally {
            if (!signal.aborted) setIsStreaming(false);
        }
        signal.throwIfAborted();
        return {
            ...askResponse,
            message: { content: answer, role: askResponse.message?.role ?? "assistant" }
        };
    };

    const makeApiRequest = async (question: string, exampleQuestionId?: string) => {
        if (!config || activeRequest.current || historyActionBusy || !question.trim()) return;
        conversationVersion.current += 1;
        if (conversationScroll.current) conversationScroll.current.scrollLeft = 0;
        const controller = new AbortController();
        activeRequest.current = controller;
        setIsRequestActive(true);
        const { signal } = controller;
        lastQuestionRef.current = question;
        setError(undefined);
        setHistoryWarning(undefined);
        setIsLoading(true);
        setMobileSidebarOpen(false);
        try {
            signal.throwIfAborted();
            const messages: ResponseMessage[] = answers.flatMap(entry => [
                { content: entry[0], role: "user" },
                { content: entry[1].message.content, role: "assistant" }
            ]);
            const request: ChatAppRequest = {
                messages: [...messages, { content: question, role: "user" }],
                context: {
                    ...(exampleQuestionId ? { example_question_id: exampleQuestionId } : {}),
                    overrides: {
                        temperature: 0.3,
                        suggest_followup_questions: suggestFollowupQuestions,
                        language: i18n.language
                    }
                },
                session_state: answers.length ? answers[answers.length - 1][1].session_state : null
            };
            const response = await chatApi(request, config.streamingEnabled === true, signal);
            if (!response.body) throw Error("No response body");
            if (response.status > 299 || !response.ok) {
                if (response.headers.get("Content-Type")?.includes("application/json")) {
                    const failure: unknown = await response.json();
                    if (failure && typeof failure === "object" && "error" in failure && typeof failure.error === "string") {
                        throw Error(failure.error);
                    }
                }
                throw Error(`Request failed with status ${response.status}`);
            }
            const parsedResponse: ChatAppResponseOrError =
                config.streamingEnabled === true ? await handleAsyncRequest(question, answers, response.body, signal) : await response.json();
            signal.throwIfAborted();
            if (parsedResponse.error) throw Error(parsedResponse.error);
            const updatedAnswers: [string, ChatAppResponse][] = [...answers, [question, parsedResponse]];
            setAnswers(updatedAnswers);
            if (historyProvider !== HistoryProviderOptions.None) {
                if (typeof parsedResponse.session_state === "string" && parsedResponse.session_state !== "") {
                    try {
                        await historyManager.addItem(parsedResponse.session_state, updatedAnswers);
                        setLatestSavedChat({
                            id: parsedResponse.session_state,
                            title: getHistoryTitle(updatedAnswers[0][0]),
                            timestamp: Date.now()
                        });
                        if (!signal.aborted && activeRequest.current === controller) setActiveHistoryId(parsedResponse.session_state);
                    } catch (historyError) {
                        console.error("Saving chat history failed:", historyError);
                        if (!signal.aborted) setHistoryWarning(t("history.saveError"));
                    }
                } else {
                    console.error("The chat response did not include a persistable session ID.");
                    setHistoryWarning(t("history.saveError"));
                }
            }
        } catch (requestError) {
            if (!signal.aborted) setError(requestError);
        } finally {
            if (activeRequest.current === controller) {
                activeRequest.current = null;
                setIsRequestActive(false);
                setIsLoading(false);
                setIsStreaming(false);
            }
        }
    };

    const startNewChat = (focusInput = false) => {
        conversationVersion.current += 1;
        if (conversationScroll.current) conversationScroll.current.scrollLeft = 0;
        activeRequest.current?.abort();
        activeRequest.current = null;
        setIsRequestActive(false);
        lastQuestionRef.current = "";
        setError(undefined);
        setHistoryWarning(undefined);
        setMobileSidebarOpen(false);
        setHistoryActionBusy(false);
        setActiveHistoryId(null);
        setLatestSavedChat(null);
        setFocusComposer(focusInput);
        setComposerVersion(version => version + 1);
        setAnswers([]);
        setStreamedAnswers([]);
        setIsLoading(false);
        setIsStreaming(false);
    };

    useEffect(() => {
        const area = conversationScroll.current;
        if (area) area.scrollTop = lastQuestionRef.current ? area.scrollHeight : 0;
    }, [isLoading, answers, streamedAnswers]);

    useEffect(() => {
        const media = window.matchMedia(DESKTOP_QUERY);
        const resize = (event: MediaQueryListEvent) => {
            setIsNarrow(!event.matches);
            if (event.matches) setMobileSidebarOpen(false);
        };
        media.addEventListener("change", resize);
        return () => media.removeEventListener("change", resize);
    }, []);

    useEffect(() => () => activeRequest.current?.abort(), []);

    const onExampleClicked = (example: string, index: number) => {
        void makeApiRequest(example, publicStatus?.example_questions?.[index] === example ? `example-${index + 1}` : undefined);
    };
    const displayedAnswers = isStreaming ? streamedAnswers : answers;
    const historyTruncated = displayedAnswers[displayedAnswers.length - 1]?.[1].context?.history_truncated;
    const hasConversation = Boolean(lastQuestionRef.current);
    const hasHistory = historyProvider !== HistoryProviderOptions.None;
    const sidebarOpen = hasHistory && (isNarrow ? mobileSidebarOpen : desktopSidebarExpanded);
    const selectionVersion = conversationVersion.current;

    useEffect(() => {
        if (!sidebarOpen && focusToggleAfterClose.current) {
            focusToggleAfterClose.current = false;
            sidebarToggle.current?.focus();
        }
    }, [sidebarOpen]);

    return (
        <div className={styles.container}>
            {sidebarOpen && (
                <ChatSidebar
                    provider={historyProvider}
                    isMobile={isNarrow}
                    conversationBusy={isRequestActive}
                    activeId={activeHistoryId}
                    latestSavedChat={latestSavedChat}
                    onBusyChange={setHistoryActionBusy}
                    onClose={() => {
                        focusToggleAfterClose.current = true;
                        if (isNarrow) setMobileSidebarOpen(false);
                        else setDesktopSidebarExpanded(false);
                    }}
                    onNewChat={() => startNewChat()}
                    onChatSelected={(id, selectedAnswers) => {
                        if (conversationVersion.current !== selectionVersion || activeRequest.current) return;
                        startNewChat();
                        setAnswers(selectedAnswers);
                        lastQuestionRef.current = selectedAnswers[selectedAnswers.length - 1][0];
                        setActiveHistoryId(id);
                    }}
                    onChatDeleted={id => {
                        if (conversationVersion.current === selectionVersion && activeHistoryId === id) startNewChat();
                    }}
                />
            )}
            <div className={styles.chatColumn}>
                {(hasHistory || hasConversation) && (!sidebarOpen || isNarrow) && (
                    <div className={styles.toolbar}>
                        {hasHistory && (
                            <button
                                ref={sidebarToggle}
                                type="button"
                                className={styles.toolbarButton}
                                aria-label={t("history.openChatHistory")}
                                title={t("history.openChatHistory")}
                                aria-expanded={sidebarOpen}
                                onClick={() => (isNarrow ? setMobileSidebarOpen(true) : setDesktopSidebarExpanded(true))}
                            >
                                <Navigation20Regular aria-hidden />
                            </button>
                        )}
                        {hasConversation && !sidebarOpen && (
                            <button
                                type="button"
                                className={styles.toolbarButton}
                                onClick={() => startNewChat(true)}
                                aria-label={t("history.newChat")}
                                title={t("history.newChat")}
                            >
                                <Add20Regular aria-hidden />
                                <span className={styles.newChatLabel}>{t("history.newChat")}</span>
                            </button>
                        )}
                    </div>
                )}
                <div ref={conversationScroll} className={styles.conversationScroll} role="region" aria-label={t("chat")} tabIndex={0}>
                    {!hasConversation ? (
                        <div className={styles.chatEmptyState}>
                            <img
                                src={getAvatarUrl(publicStatus)}
                                alt={t("profile.avatar", { owner: publicStatus?.owner_name || getAssistantName(publicStatus) })}
                                className={styles.chatEmptyStateAvatar}
                            />
                            <h1 className={styles.chatEmptyStateTitle}>{getAssistantName(publicStatus)}</h1>
                            <p className={styles.welcomeMessage}>{publicStatus?.welcome_message?.trim() || t("chatEmptyStateSubtitle")}</p>
                            {config?.showLanguagePicker && <LanguagePicker onLanguageChange={newLang => i18n.changeLanguage(newLang)} />}
                            {config && <ExampleList onExampleClicked={onExampleClicked} questions={publicStatus?.example_questions} />}
                        </div>
                    ) : (
                        <div className={styles.chatMessageStream}>
                            <h1 className={styles.visuallyHidden}>{t("chat")}</h1>
                            {displayedAnswers.map((answer, index) => (
                                <div key={index}>
                                    <UserChatMessage message={answer[0]} />
                                    <div className={styles.chatMessageGpt}>
                                        <Answer
                                            isStreaming={isStreaming}
                                            answer={answer[1]}
                                            onFollowupQuestionClicked={question => makeApiRequest(question)}
                                            showFollowupQuestions={suggestFollowupQuestions && answers.length - 1 === index}
                                        />
                                    </div>
                                </div>
                            ))}
                            {isLoading && (
                                <>
                                    <UserChatMessage message={lastQuestionRef.current} />
                                    <div className={styles.chatMessageGptMinWidth}>
                                        <AnswerLoading />
                                    </div>
                                </>
                            )}
                            {error ? (
                                <>
                                    <UserChatMessage message={lastQuestionRef.current} />
                                    <div className={styles.chatMessageGptMinWidth}>
                                        <AnswerError error={error.toString()} onRetry={() => makeApiRequest(lastQuestionRef.current)} />
                                    </div>
                                </>
                            ) : null}
                        </div>
                    )}
                </div>
                <div className={styles.chatInput}>
                    {historyTruncated && <p role="status">{t("contextTruncated")}</p>}
                    {historyWarning && (
                        <p role="status" className={styles.historyWarning}>
                            {historyWarning}
                        </p>
                    )}
                    {configError ? (
                        <div role="alert">
                            <AnswerError error={t("configuration.error")} onRetry={retryConfig} />
                        </div>
                    ) : !config ? (
                        <Spinner label={t("configuration.loading")} />
                    ) : null}
                    <QuestionInput
                        key={composerVersion}
                        autoFocus={focusComposer}
                        clearOnSend
                        placeholder={t("defaultExamples.placeholder")}
                        disabled={!config || isRequestActive || historyActionBusy}
                        onSend={question => makeApiRequest(question)}
                    />
                    <p className={styles.privacyDisclosure}>{t("privacy.disclosure")}</p>
                    {hasHistory && !sidebarOpen && <p className={styles.privacyDisclosure}>{t("privacy.localHistory")}</p>}
                </div>
            </div>
        </div>
    );
};

export default Chat;
