import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import styles from "./VoiceChat.module.css";
import { useRealtimeWebSocket } from "./useRealtimeWebSocket";
import type { UserVoiceTurn } from "./useRealtimeWebSocket";
import { useAudioCapture } from "./useAudioCapture";
import { useAudioPlayback } from "./useAudioPlayback";
import { configApi } from "../../api";

interface ConversationEntry {
    role: "user" | "assistant";
    content: string;
    itemId?: string;
}

export function Component() {
    const navigate = useNavigate();
    const { t } = useTranslation();
    const [voiceEnabled, setVoiceEnabled] = useState<boolean | null>(null);
    const [configError, setConfigError] = useState(false);
    const [conversation, setConversation] = useState<ConversationEntry[]>([]);
    const [isSessionActive, setIsSessionActive] = useState(false);
    const [sessionError, setSessionError] = useState<string | null>(null);
    const sessionGenerationRef = useRef(0);
    const sessionActiveRef = useRef(false);
    const mountedRef = useRef(false);
    const transcriptEndRef = useRef<HTMLDivElement>(null);

    const { playAudioChunk, stopPlayback, resetPlaybackTime } = useAudioPlayback();
    const { isCapturing, startCapture, stopCapture, setOnAudioChunk } = useAudioCapture();

    const stopAudio = useCallback(() => {
        const generation = ++sessionGenerationRef.current;
        sessionActiveRef.current = false;
        if (mountedRef.current) setIsSessionActive(false);
        void Promise.all([stopCapture(), stopPlayback()]).catch(() => {
            if (mountedRef.current && sessionGenerationRef.current === generation) {
                setSessionError(t("voice.releaseError"));
            }
        });
    }, [stopCapture, stopPlayback, t]);

    useEffect(() => {
        const controller = new AbortController();
        configApi(controller.signal)
            .then(config => {
                if (!controller.signal.aborted) setVoiceEnabled(config.showRealtimeVoice === true);
            })
            .catch(() => {
                if (!controller.signal.aborted) {
                    setConfigError(true);
                    setVoiceEnabled(false);
                }
            });
        return () => controller.abort();
    }, []);

    const recordVoiceTurn = useCallback((turn: UserVoiceTurn, previousAssistantReply: string) => {
        setConversation(previous => [
            ...previous,
            ...(previousAssistantReply ? [{ role: "assistant" as const, content: previousAssistantReply }] : []),
            { role: "user", content: turn.transcript, itemId: turn.itemId }
        ]);
    }, []);

    const updateUserTranscript = useCallback((turn: UserVoiceTurn) => {
        setConversation(previous =>
            previous.map(entry => (entry.role === "user" && entry.itemId === turn.itemId ? { ...entry, content: turn.transcript } : entry))
        );
    }, []);

    const recordAssistantReply = useCallback((content: string) => {
        setConversation(previous => [...previous, { role: "assistant", content }]);
    }, []);

    const { status, error, closeReason, transcript, isModelSpeaking, isProcessing, connect, disconnect, sendAudioChunk } = useRealtimeWebSocket({
        onAudioDelta: playAudioChunk,
        onDisconnected: stopAudio,
        onUserVoiceTurn: recordVoiceTurn,
        onUserTranscript: updateUserTranscript,
        onAssistantReplyCompleted: recordAssistantReply
    });

    const stopSession = useCallback(() => {
        stopAudio();
        disconnect();
    }, [stopAudio, disconnect]);

    // Wire audio capture to WebSocket
    useEffect(() => {
        setOnAudioChunk(sendAudioChunk);
    }, [setOnAudioChunk, sendAudioChunk]);

    // Auto-scroll transcript
    useEffect(() => {
        transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }, [conversation, transcript]);

    const handleToggle = useCallback(async () => {
        if (sessionActiveRef.current) {
            stopSession();
            return;
        }
        if (!voiceEnabled) return;

        const generation = ++sessionGenerationRef.current;
        sessionActiveRef.current = true;
        setIsSessionActive(true);
        setSessionError(null);
        let startError = t("voice.connectionError");

        try {
            setConversation([]);
            resetPlaybackTime();

            await connect();
            if (sessionGenerationRef.current !== generation) return;
            startError = t("voice.microphoneError");
            await startCapture();
        } catch {
            if (mountedRef.current && sessionGenerationRef.current === generation) {
                stopSession();
                setSessionError(startError);
            }
        }
    }, [connect, startCapture, stopSession, resetPlaybackTime, voiceEnabled, t]);

    // Clean up voice session on unmount (e.g. navigating to text chat)
    useEffect(() => {
        mountedRef.current = true;
        return () => {
            mountedRef.current = false;
            stopSession();
        };
    }, [stopSession]);

    const statusLabel = (() => {
        if (voiceEnabled === null) return t("configuration.loading");
        if (!voiceEnabled) return t("voice.unavailable");
        if (sessionError) return t("voice.sessionError");
        switch (status) {
            case "connecting":
                return t("voice.connecting");
            case "connected":
                return isModelSpeaking
                    ? t("voice.speaking")
                    : isProcessing
                      ? t("voice.processing")
                      : isCapturing
                        ? t("voice.listening")
                        : t("voice.startingMicrophone");
            case "error":
                return t("voice.connectionErrorLabel");
            default:
                return isSessionActive ? t("voice.connecting") : closeReason || t("voice.disconnected");
        }
    })();

    const statusDotClass = (() => {
        if (sessionError) return styles.statusDotError;
        switch (status) {
            case "connecting":
                return styles.statusDotConnecting;
            case "connected":
                return styles.statusDotConnected;
            case "error":
                return styles.statusDotError;
            default:
                return styles.statusDot;
        }
    })();

    return (
        <div className={styles.container}>
            <h1 className={styles.title}>{t("voice.title")}</h1>
            <p className={styles.disclosure}>{t("privacy.voice")}</p>
            <p className={styles.disclosure}>{t("voice.transcriptNotice")}</p>
            <div className={styles.statusBar}>
                <div className={statusDotClass} />
                <span>{statusLabel}</span>
                {isModelSpeaking && (
                    <div className={styles.speakingIndicator}>
                        <div className={styles.speakingDot} />
                        <div className={styles.speakingDot} />
                        <div className={styles.speakingDot} />
                    </div>
                )}
            </div>

            {(sessionError || error || configError) && (
                <div className={styles.errorMessage} role="alert">
                    {sessionError || error || t("configuration.error")}
                </div>
            )}

            <div className={styles.transcriptArea}>
                {conversation.length === 0 && !transcript ? (
                    <div className={styles.emptyTranscript}>{isCapturing ? t("voice.listeningPrompt") : t("voice.startPrompt")}</div>
                ) : (
                    <>
                        {conversation.map((entry, index) => (
                            <div key={index} className={entry.role === "user" ? styles.userBubble : styles.assistantBubble}>
                                {entry.role === "user" ? entry.content.trim() || t("voice.voiceMessage") : entry.content}
                            </div>
                        ))}
                        {/* Live transcript being generated */}
                        {transcript && <div className={styles.assistantBubble}>{transcript}</div>}
                    </>
                )}
                <div ref={transcriptEndRef} />
            </div>

            <div className={styles.controls}>
                <button className={isSessionActive ? styles.micButtonActive : styles.micButton} onClick={handleToggle} disabled={!voiceEnabled}>
                    {t(isSessionActive ? "voice.stop" : "voice.start")}
                </button>
                <button className={styles.backButton} onClick={() => navigate("/")}>
                    {t("voice.back")}
                </button>
            </div>
        </div>
    );
}

Component.displayName = "VoiceChat";
