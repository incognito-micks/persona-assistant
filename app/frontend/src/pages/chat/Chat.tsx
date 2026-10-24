import { useRef, useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { Spinner } from "@fluentui/react";
import readNDJSONStream from "ndjson-readablestream";

import styles from "./Chat.module.css";

import { chatApi, configApi, ChatAppResponse, ChatAppResponseOrError, ChatAppRequest, Config, ResponseMessage } from "../../api";
import { Answer, AnswerError, AnswerLoading } from "../../components/Answer";
import { QuestionInput } from "../../components/QuestionInput";
import { ExampleList } from "../../components/Example";
import { UserChatMessage } from "../../components/UserChatMessage";
import { HistoryPanel } from "../../components/HistoryPanel";
import { HistoryProviderOptions, useHistoryManager } from "../../components/HistoryProviders";
import { HistoryButton } from "../../components/HistoryButton";
import { ClearChatButton } from "../../components/ClearChatButton";
import { useAppContext } from "../../appContext";
import { getAssistantName, getAvatarUrl } from "../../profile";
import { LanguagePicker } from "../../i18n/LanguagePicker";

const suggestFollowupQuestions = false;

const Chat = () => {
    const navigate = useNavigate();
    const { t, i18n } = useTranslation();
    const { publicStatus } = useAppContext();
    const [config, setConfig] = useState<Config>();
    const [configError, setConfigError] = useState<string>();
    const [configAttempt, setConfigAttempt] = useState(0);

    const lastQuestionRef = useRef<string>("");
    const chatMessageStreamEnd = useRef<HTMLDivElement | null>(null);
    const activeRequest = useRef<AbortController | null>(null);

    const [isLoading, setIsLoading] = useState<boolean>(false);
    const [isStreaming, setIsStreaming] = useState<boolean>(false);
    const [error, setError] = useState<unknown>();

    const [answers, setAnswers] = useState<[user: string, response: ChatAppResponse][]>([]);
    const [streamedAnswers, setStreamedAnswers] = useState<[user: string, response: ChatAppResponse][]>([]);

    const [isHistoryOpen, setIsHistoryOpen] = useState(false);

    const handleAsyncRequest = async (
        question: string,
        previousAnswers: [string, ChatAppResponse][],
        responseBody: ReadableStream<Uint8Array>,
        signal: AbortSignal
    ) => {
        let answer: string = "";
        let askResponse: ChatAppResponse = {} as ChatAppResponse;

        const updateState = async (newContent: string) => {
            await new Promise(resolve => setTimeout(resolve, 33));
            signal.throwIfAborted();
            answer += newContent;
            const latestResponse: ChatAppResponse = {
                ...askResponse,
                message: { content: answer, role: askResponse.message?.role ?? "assistant" }
            };
            setStreamedAnswers([...previousAnswers, [question, latestResponse]]);
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
                    // Update context with new keys from latest event
                    askResponse.context = { ...askResponse.context, ...event["context"] };
                } else if (event["error"]) {
                    throw Error(event["error"]);
                }
            }
        } finally {
            if (!signal.aborted) {
                setIsStreaming(false);
            }
        }
        signal.throwIfAborted();
        const fullResponse: ChatAppResponse = {
            ...askResponse,
            message: { content: answer, role: askResponse.message?.role ?? "assistant" }
        };
        return fullResponse;
    };

    const historyProvider = config?.showChatHistoryBrowser ? HistoryProviderOptions.IndexedDB : HistoryProviderOptions.None;
    const historyManager = useHistoryManager(historyProvider);

    const makeApiRequest = async (question: string, exampleQuestionId?: string) => {
        if (!config || activeRequest.current || !question.trim()) return;

        const controller = new AbortController();
        activeRequest.current = controller;
        const { signal } = controller;
        const shouldStream = config.streamingEnabled === true;
        lastQuestionRef.current = question;

        setError(undefined);
        setIsLoading(true);
        setIsHistoryOpen(false);

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
                // AI Chat Protocol: Client must pass on any session state received from the server
                session_state: answers.length ? answers[answers.length - 1][1].session_state : null
            };

            const response = await chatApi(request, shouldStream, signal);
            if (!response.body) {
                throw Error("No response body");
            }
            if (response.status > 299 || !response.ok) {
                if (response.headers.get("Content-Type")?.includes("application/json")) {
                    const failure: unknown = await response.json();
                    if (failure && typeof failure === "object" && "error" in failure && typeof failure.error === "string") {
                        throw Error(failure.error);
                    }
                }
                throw Error(`Request failed with status ${response.status}`);
            }
            const parsedResponse: ChatAppResponseOrError = shouldStream
                ? await handleAsyncRequest(question, answers, response.body, signal)
                : await response.json();
            signal.throwIfAborted();
            if (parsedResponse.error) {
                throw Error(parsedResponse.error);
            }
            const updatedAnswers: [string, ChatAppResponse][] = [...answers, [question, parsedResponse]];
            setAnswers(updatedAnswers);
            if (typeof parsedResponse.session_state === "string" && parsedResponse.session_state !== "") {
                try {
                    await historyManager.addItem(parsedResponse.session_state, updatedAnswers);
                } catch (historyError) {
                    console.error("Saving chat history failed:", historyError);
                }
            }
        } catch (requestError) {
            if (!signal.aborted) {
                setError(requestError);
            }
        } finally {
            if (activeRequest.current === controller) {
                activeRequest.current = null;
                setIsLoading(false);
                setIsStreaming(false);
            }
        }
    };

    const clearChat = () => {
        activeRequest.current?.abort();
        activeRequest.current = null;
        lastQuestionRef.current = "";
        setError(undefined);
        setIsHistoryOpen(false);
        setAnswers([]);
        setStreamedAnswers([]);
        setIsLoading(false);
        setIsStreaming(false);
    };

    useEffect(() => {
        chatMessageStreamEnd.current?.scrollIntoView({ behavior: "smooth" });
    }, [isLoading]);
    useEffect(() => {
        chatMessageStreamEnd.current?.scrollIntoView({ behavior: "auto" });
    }, [streamedAnswers]);
    useEffect(() => {
        const controller = new AbortController();
        setConfigError(undefined);
        configApi(controller.signal)
            .then(loadedConfig => {
                if (!controller.signal.aborted) setConfig(loadedConfig);
            })
            .catch(() => {
                if (!controller.signal.aborted) setConfigError(t("configuration.error"));
            });
        return () => controller.abort();
    }, [configAttempt, t]);

    useEffect(() => {
        return () => {
            activeRequest.current?.abort();
        };
    }, []);

    const onExampleClicked = (example: string, index: number) => {
        makeApiRequest(example, publicStatus?.example_questions?.[index] === example ? `example-${index + 1}` : undefined);
    };

    const displayedAnswers = isStreaming ? streamedAnswers : answers;
    const historyTruncated = displayedAnswers[displayedAnswers.length - 1]?.[1].context.history_truncated;

    return (
        <div className={styles.container}>
            <div className={styles.commandsSplitContainer}>
                <div className={styles.commandsContainer}>
                    <ClearChatButton className={styles.commandButton} onClick={clearChat} disabled={!lastQuestionRef.current} />
                    {historyProvider !== HistoryProviderOptions.None && (
                        <HistoryButton
                            className={styles.commandButton}
                            disabled={isLoading || isStreaming}
                            isOpen={isHistoryOpen}
                            onClick={() => setIsHistoryOpen(open => !open)}
                        />
                    )}
                </div>
            </div>
            <div className={styles.chatRoot}>
                <div className={styles.chatContainer}>
                    {!lastQuestionRef.current ? (
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
                            {config?.showRealtimeVoice && (
                                <button className={styles.voiceChatButton} onClick={() => navigate("/voice")}>
                                    {t("voice.open")}
                                </button>
                            )}
                        </div>
                    ) : (
                        <div className={styles.chatMessageStream}>
                            {isStreaming &&
                                streamedAnswers.map((streamedAnswer, index) => (
                                    <div key={index}>
                                        <UserChatMessage message={streamedAnswer[0]} />
                                        <div className={styles.chatMessageGpt}>
                                            <Answer
                                                isStreaming={true}
                                                key={index}
                                                answer={streamedAnswer[1]}
                                                onFollowupQuestionClicked={q => makeApiRequest(q)}
                                                showFollowupQuestions={suggestFollowupQuestions && answers.length - 1 === index}
                                            />
                                        </div>
                                    </div>
                                ))}
                            {!isStreaming &&
                                answers.map((answer, index) => (
                                    <div key={index}>
                                        <UserChatMessage message={answer[0]} />
                                        <div className={styles.chatMessageGpt}>
                                            <Answer
                                                isStreaming={false}
                                                key={index}
                                                answer={answer[1]}
                                                onFollowupQuestionClicked={q => makeApiRequest(q)}
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
                            <div ref={chatMessageStreamEnd} />
                        </div>
                    )}

                    <div className={styles.chatInput}>
                        {historyTruncated && <p role="status">{t("contextTruncated")}</p>}
                        {configError ? (
                            <div role="alert">
                                <AnswerError error={configError} onRetry={() => setConfigAttempt(attempt => attempt + 1)} />
                            </div>
                        ) : !config ? (
                            <Spinner label={t("configuration.loading")} />
                        ) : null}
                        <QuestionInput
                            clearOnSend
                            placeholder={t("defaultExamples.placeholder")}
                            disabled={!config || isLoading || isStreaming}
                            onSend={question => makeApiRequest(question)}
                        />
                        {config?.showRealtimeVoice && lastQuestionRef.current && (
                            <button className={styles.voiceChatButton} onClick={() => navigate("/voice")}>
                                {t("voice.open")}
                            </button>
                        )}
                        <p className={styles.privacyDisclosure}>{t("privacy.disclosure")}</p>
                        {historyProvider === HistoryProviderOptions.IndexedDB && <p className={styles.privacyDisclosure}>{t("privacy.localHistory")}</p>}
                    </div>
                </div>

                {historyProvider !== HistoryProviderOptions.None && isHistoryOpen && (
                    <HistoryPanel
                        provider={historyProvider}
                        isOpen={isHistoryOpen}
                        onClose={() => setIsHistoryOpen(false)}
                        onChatSelected={selectedAnswers => {
                            if (selectedAnswers.length === 0) return;
                            clearChat();
                            setAnswers(selectedAnswers);
                            lastQuestionRef.current = selectedAnswers[selectedAnswers.length - 1][0];
                        }}
                    />
                )}
            </div>
        </div>
    );
};

export default Chat;
