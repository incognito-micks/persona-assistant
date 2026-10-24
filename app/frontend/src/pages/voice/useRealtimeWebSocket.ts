import { useCallback, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

export type ConnectionStatus = "disconnected" | "connecting" | "connected" | "error";

export interface UserVoiceTurn {
    itemId: string;
    transcript: string;
}

interface InputAudioTurn extends UserVoiceTurn {
    committed: boolean;
    complete: boolean;
}

interface UseRealtimeWebSocketOptions {
    onAudioDelta?: (base64Audio: string) => void;
    onDisconnected?: () => void;
    onUserVoiceTurn?: (turn: UserVoiceTurn, previousAssistantReply: string) => void;
    onUserTranscript?: (turn: UserVoiceTurn) => void;
    onAssistantReplyCompleted?: (transcript: string) => void;
}

export function useRealtimeWebSocket({
    onAudioDelta,
    onDisconnected,
    onUserVoiceTurn,
    onUserTranscript,
    onAssistantReplyCompleted
}: UseRealtimeWebSocketOptions = {}) {
    const { t } = useTranslation();
    const [status, setStatus] = useState<ConnectionStatus>("disconnected");
    const [error, setError] = useState<string | null>(null);
    const [closeReason, setCloseReason] = useState<string | null>(null);
    const [transcript, setTranscript] = useState("");
    const [isModelSpeaking, setIsModelSpeaking] = useState(false);
    const [isProcessing, setIsProcessing] = useState(false);

    const wsRef = useRef<WebSocket | null>(null);
    const transcriptRef = useRef("");
    const inputTurnsRef = useRef(new Map<string, InputAudioTurn>());
    const cancelConnectRef = useRef<(() => void) | null>(null);
    const onAudioDeltaRef = useRef(onAudioDelta);
    onAudioDeltaRef.current = onAudioDelta;
    const onDisconnectedRef = useRef(onDisconnected);
    onDisconnectedRef.current = onDisconnected;
    const onUserVoiceTurnRef = useRef(onUserVoiceTurn);
    onUserVoiceTurnRef.current = onUserVoiceTurn;
    const onUserTranscriptRef = useRef(onUserTranscript);
    onUserTranscriptRef.current = onUserTranscript;
    const onAssistantReplyCompletedRef = useRef(onAssistantReplyCompleted);
    onAssistantReplyCompletedRef.current = onAssistantReplyCompleted;

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
        inputTurnsRef.current.clear();

        const getInputTurn = (itemId: unknown) => {
            if (typeof itemId !== "string" || !itemId.trim() || itemId.length > 256) return;
            let turn = inputTurnsRef.current.get(itemId);
            if (!turn) {
                turn = { itemId, transcript: "", committed: false, complete: false };
                inputTurnsRef.current.set(itemId, turn);
            }
            return turn;
        };

        const updateInputTranscript = (itemId: unknown, text: string, complete: boolean) => {
            const turn = getInputTurn(itemId);
            if (!turn || turn.complete) return;
            turn.transcript = complete ? text : turn.transcript + text;
            turn.complete = complete;
            if (turn.committed) onUserTranscriptRef.current?.({ itemId: turn.itemId, transcript: turn.transcript });
        };

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

                    case "conversation.item.input_audio_transcription.delta":
                        if (typeof data.delta === "string") {
                            updateInputTranscript(data.item_id, data.delta, false);
                        }
                        break;

                    case "conversation.item.input_audio_transcription.completed":
                        if (typeof data.transcript === "string") {
                            updateInputTranscript(data.item_id, data.transcript, true);
                        }
                        break;

                    case "conversation.item.input_audio_transcription.failed":
                        updateInputTranscript(data.item_id, "", true);
                        break;

                    case "response.created":
                        if (transcriptRef.current) onAssistantReplyCompletedRef.current?.(transcriptRef.current);
                        transcriptRef.current = "";
                        setTranscript("");
                        setIsProcessing(false);
                        break;

                    case "input_audio_buffer.speech_started":
                        setIsProcessing(false);
                        break;

                    case "input_audio_buffer.speech_stopped":
                        setIsProcessing(true);
                        break;

                    case "input_audio_buffer.committed": {
                        const turn = getInputTurn(data.item_id);
                        if (!turn || turn.committed) break;
                        turn.committed = true;
                        onUserVoiceTurnRef.current?.({ itemId: turn.itemId, transcript: turn.transcript }, transcriptRef.current);
                        transcriptRef.current = "";
                        setTranscript("");
                        setIsProcessing(true);
                        break;
                    }
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
        isModelSpeaking,
        isProcessing,
        connect,
        disconnect,
        sendAudioChunk
    };
}
