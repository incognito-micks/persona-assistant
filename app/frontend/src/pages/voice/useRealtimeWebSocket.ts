import { useCallback, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

export type ConnectionStatus = "disconnected" | "connecting" | "connected" | "error";

interface UseRealtimeWebSocketOptions {
    onAudioDelta?: (base64Audio: string) => void;
    onDisconnected?: () => void;
    onUserVoiceTurn?: (previousAssistantReply: string) => void;
}

export function useRealtimeWebSocket({ onAudioDelta, onDisconnected, onUserVoiceTurn }: UseRealtimeWebSocketOptions = {}) {
    const { t } = useTranslation();
    const [status, setStatus] = useState<ConnectionStatus>("disconnected");
    const [error, setError] = useState<string | null>(null);
    const [closeReason, setCloseReason] = useState<string | null>(null);
    const [transcript, setTranscript] = useState("");
    const [userTranscript, setUserTranscript] = useState("");
    const [isModelSpeaking, setIsModelSpeaking] = useState(false);
    const [isProcessing, setIsProcessing] = useState(false);

    const wsRef = useRef<WebSocket | null>(null);
    const transcriptRef = useRef("");
    const committedTurnIdsRef = useRef(new Set<string>());
    const cancelConnectRef = useRef<(() => void) | null>(null);
    const onAudioDeltaRef = useRef(onAudioDelta);
    onAudioDeltaRef.current = onAudioDelta;
    const onDisconnectedRef = useRef(onDisconnected);
    onDisconnectedRef.current = onDisconnected;
    const onUserVoiceTurnRef = useRef(onUserVoiceTurn);
    onUserVoiceTurnRef.current = onUserVoiceTurn;

    const disconnect = useCallback(() => {
        const ws = wsRef.current;
        wsRef.current = null;
        const cancelConnect = cancelConnectRef.current;
        cancelConnectRef.current = null;
        cancelConnect?.();
        if (ws && ws.readyState < WebSocket.CLOSING) ws.close();
        setStatus("disconnected");
        setError(null);
        setCloseReason(null);
        setIsModelSpeaking(false);
        setIsProcessing(false);
    }, []);

    const connect = useCallback(() => {
        disconnect();
        setStatus("connecting");
        setTranscript("");
        transcriptRef.current = "";
        committedTurnIdsRef.current.clear();
        setUserTranscript("");

        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const url = `${protocol}//${window.location.host}/realtime`;
        const connectionErrorMessage = t("voice.connectionError");

        return new Promise<void>((resolve, reject) => {
            let ws: WebSocket;
            try {
                ws = new WebSocket(url);
            } catch {
                setStatus("error");
                setError(connectionErrorMessage);
                onDisconnectedRef.current?.();
                reject(new Error(connectionErrorMessage));
                return;
            }
            wsRef.current = ws;
            let opened = false;
            let settled = false;
            let failureMessage: string | null = null;

            const settle = (connectionError?: Error) => {
                if (settled) return;
                settled = true;
                if (wsRef.current === ws) cancelConnectRef.current = null;
                if (connectionError) reject(connectionError);
                else resolve();
            };
            cancelConnectRef.current = () => settle(new DOMException("Voice connection cancelled", "AbortError"));

            const failConnection = (message: string) => {
                if (wsRef.current !== ws || failureMessage) return;
                failureMessage = message;
                setStatus("error");
                setError(message);
                setIsModelSpeaking(false);
                setIsProcessing(false);
                settle(new Error(message));
                onDisconnectedRef.current?.();
                ws.close();
            };

            ws.onopen = () => {
                if (wsRef.current !== ws || failureMessage) return;
                opened = true;
                setStatus("connected");
                settle();
            };

            ws.onclose = (event: CloseEvent) => {
                if (wsRef.current !== ws) return;
                const failed = Boolean(failureMessage) || !opened || (event.code !== 1000 && event.code !== 1001);
                const message = event.reason || failureMessage || connectionErrorMessage;
                settle(new Error(message));
                wsRef.current = null;
                setStatus(failed ? "error" : "disconnected");
                setError(failed ? message : null);
                setCloseReason(event.reason || null);
                setIsModelSpeaking(false);
                setIsProcessing(false);
                onDisconnectedRef.current?.();
            };

            ws.onerror = () => failConnection(connectionErrorMessage);

            ws.onmessage = (event: MessageEvent) => {
                if (wsRef.current !== ws || failureMessage || ws.readyState !== WebSocket.OPEN) return;
                let data: { type?: string; delta?: string; transcript?: string; item_id?: unknown };
                try {
                    data = JSON.parse(event.data);
                } catch {
                    // Ignore non-JSON messages.
                    return;
                }
                if (!data || typeof data !== "object") return;

                switch (data.type) {
                    case "response.audio.delta":
                        setIsModelSpeaking(true);
                        setIsProcessing(false);
                        if (typeof data.delta === "string" && data.delta) {
                            try {
                                onAudioDeltaRef.current?.(data.delta);
                            } catch {
                                failConnection(t("voice.playbackError"));
                            }
                        }
                        break;

                    case "response.audio.done":
                        setIsModelSpeaking(false);
                        break;

                    case "response.audio_transcript.delta":
                        if (typeof data.delta === "string") {
                            transcriptRef.current += data.delta;
                            setTranscript(transcriptRef.current);
                            setIsProcessing(false);
                        }
                        break;

                    case "conversation.item.input_audio_transcription.completed":
                        if (typeof data.transcript === "string") {
                            setUserTranscript(data.transcript);
                        }
                        break;

                    case "response.created":
                        transcriptRef.current = "";
                        setTranscript("");
                        setIsProcessing(false);
                        break;

                    case "input_audio_buffer.speech_started":
                        setUserTranscript("");
                        setIsProcessing(false);
                        break;

                    case "input_audio_buffer.speech_stopped":
                        setIsProcessing(true);
                        break;

                    case "input_audio_buffer.committed":
                        if (
                            typeof data.item_id !== "string" ||
                            !data.item_id.trim() ||
                            data.item_id.length > 256 ||
                            committedTurnIdsRef.current.has(data.item_id)
                        ) {
                            break;
                        }
                        committedTurnIdsRef.current.add(data.item_id);
                        onUserVoiceTurnRef.current?.(transcriptRef.current);
                        transcriptRef.current = "";
                        setTranscript("");
                        setIsProcessing(true);
                        break;
                }
            };
        });
    }, [disconnect, t]);

    const sendAudioChunk = useCallback((base64Audio: string) => {
        if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
            wsRef.current.send(
                JSON.stringify({
                    type: "input_audio_buffer.append",
                    audio: base64Audio
                })
            );
        }
    }, []);

    return {
        status,
        error,
        closeReason,
        transcript,
        userTranscript,
        isModelSpeaking,
        isProcessing,
        connect,
        disconnect,
        sendAudioChunk
    };
}
