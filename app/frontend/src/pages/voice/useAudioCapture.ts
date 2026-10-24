import { useCallback, useRef, useState } from "react";

// AudioWorklet processor code as a string - will be loaded via Blob URL
const WORKLET_PROCESSOR_CODE = `
const BASE64_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

function uint8ArrayToBase64(bytes) {
    let result = '';
    const len = bytes.length;
    for (let i = 0; i < len; i += 3) {
        const b0 = bytes[i];
        const b1 = i + 1 < len ? bytes[i + 1] : 0;
        const b2 = i + 2 < len ? bytes[i + 2] : 0;
        result += BASE64_CHARS[b0 >> 2];
        result += BASE64_CHARS[((b0 & 3) << 4) | (b1 >> 4)];
        result += i + 1 < len ? BASE64_CHARS[((b1 & 15) << 2) | (b2 >> 6)] : '=';
        result += i + 2 < len ? BASE64_CHARS[b2 & 63] : '=';
    }
    return result;
}

class PCM16CaptureProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this._buffer = [];
        this._bufferSize = 2400; // ~100ms at 24kHz
    }

    process(inputs) {
        const input = inputs[0];
        if (input.length === 0) return true;

        const channelData = input[0];
        for (let i = 0; i < channelData.length; i++) {
            this._buffer.push(channelData[i]);
        }

        while (this._buffer.length >= this._bufferSize) {
            const chunk = this._buffer.splice(0, this._bufferSize);
            // Convert float32 to PCM16
            const pcm16 = new Int16Array(chunk.length);
            for (let i = 0; i < chunk.length; i++) {
                const s = Math.max(-1, Math.min(1, chunk[i]));
                pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
            }
            this.port.postMessage(uint8ArrayToBase64(new Uint8Array(pcm16.buffer)));
        }

        return true;
    }
}

registerProcessor('pcm16-capture-processor', PCM16CaptureProcessor);
`;

interface CaptureResources {
    stream: MediaStream | null;
    audioContext: AudioContext | null;
    source: MediaStreamAudioSourceNode | null;
    workletNode: AudioWorkletNode | null;
    workletUrl: string | null;
}

async function releaseCaptureResources(capture: CaptureResources) {
    capture.stream?.getTracks().forEach(track => track.stop());
    capture.stream = null;
    if (capture.workletNode) {
        capture.workletNode.port.onmessage = null;
        capture.workletNode.port.close();
        capture.workletNode.disconnect();
        capture.workletNode = null;
    }
    capture.source?.disconnect();
    capture.source = null;
    if (capture.workletUrl) {
        URL.revokeObjectURL(capture.workletUrl);
        capture.workletUrl = null;
    }
    const audioContext = capture.audioContext;
    capture.audioContext = null;
    if (audioContext && audioContext.state !== "closed") {
        await audioContext.close();
    }
}

export function useAudioCapture() {
    const [isCapturing, setIsCapturing] = useState(false);
    const captureRef = useRef<CaptureResources | null>(null);
    const onAudioChunkRef = useRef<((base64: string) => void) | null>(null);

    const startCapture = useCallback(async () => {
        if (captureRef.current) return;

        const capture: CaptureResources = {
            stream: null,
            audioContext: null,
            source: null,
            workletNode: null,
            workletUrl: null
        };
        captureRef.current = capture;

        try {
            const stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    sampleRate: 24000,
                    channelCount: 1,
                    echoCancellation: true,
                    noiseSuppression: true
                }
            });
            capture.stream = stream;
            if (captureRef.current !== capture) return;

            const audioContext = new AudioContext({ sampleRate: 24000 });
            capture.audioContext = audioContext;

            // Load worklet processor from Blob URL
            const blob = new Blob([WORKLET_PROCESSOR_CODE], { type: "application/javascript" });
            const workletUrl = URL.createObjectURL(blob);
            capture.workletUrl = workletUrl;
            try {
                await audioContext.audioWorklet.addModule(workletUrl);
            } finally {
                if (capture.workletUrl) {
                    URL.revokeObjectURL(capture.workletUrl);
                    capture.workletUrl = null;
                }
            }
            if (captureRef.current !== capture) return;

            const source = audioContext.createMediaStreamSource(stream);
            capture.source = source;

            const workletNode = new AudioWorkletNode(audioContext, "pcm16-capture-processor");
            capture.workletNode = workletNode;

            workletNode.port.onmessage = (event: MessageEvent) => {
                if (captureRef.current === capture && onAudioChunkRef.current) {
                    onAudioChunkRef.current(event.data);
                }
            };

            source.connect(workletNode);
            workletNode.connect(audioContext.destination);

            setIsCapturing(true);
        } catch (err) {
            // Cancelled permission/worklet requests can still reject after cleanup.
            if (captureRef.current !== capture) return;
            captureRef.current = null;
            setIsCapturing(false);
            throw err;
        } finally {
            if (captureRef.current !== capture) {
                await releaseCaptureResources(capture);
            }
        }
    }, []);

    const stopCapture = useCallback(async () => {
        const capture = captureRef.current;
        captureRef.current = null;
        setIsCapturing(false);
        if (capture) await releaseCaptureResources(capture);
    }, []);

    const setOnAudioChunk = useCallback((callback: (base64: string) => void) => {
        onAudioChunkRef.current = callback;
    }, []);

    return {
        isCapturing,
        startCapture,
        stopCapture,
        setOnAudioChunk
    };
}
