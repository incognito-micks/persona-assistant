import { useCallback, useRef } from "react";

export function useAudioPlayback() {
    const audioContextRef = useRef<AudioContext | null>(null);
    const nextStartTimeRef = useRef(0);

    const getAudioContext = useCallback(() => {
        if (!audioContextRef.current || audioContextRef.current.state === "closed") {
            audioContextRef.current = new AudioContext({ sampleRate: 24000 });
            nextStartTimeRef.current = 0;
        }
        return audioContextRef.current;
    }, []);

    const playAudioChunk = useCallback(
        (base64Audio: string) => {
            const ctx = getAudioContext();

            // Decode base64 to PCM16 bytes
            const binaryString = atob(base64Audio);
            const bytes = new Uint8Array(binaryString.length);
            for (let i = 0; i < binaryString.length; i++) {
                bytes[i] = binaryString.charCodeAt(i);
            }

            // Convert PCM16 to float32
            const pcm16 = new Int16Array(bytes.buffer);
            const float32 = new Float32Array(pcm16.length);
            for (let i = 0; i < pcm16.length; i++) {
                float32[i] = pcm16[i] / (pcm16[i] < 0 ? 0x8000 : 0x7fff);
            }

            // Create AudioBuffer and schedule playback
            const audioBuffer = ctx.createBuffer(1, float32.length, 24000);
            audioBuffer.getChannelData(0).set(float32);

            const source = ctx.createBufferSource();
            source.buffer = audioBuffer;
            source.connect(ctx.destination);

            const now = ctx.currentTime;
            const startTime = Math.max(now, nextStartTimeRef.current);
            source.start(startTime);
            nextStartTimeRef.current = startTime + audioBuffer.duration;
        },
        [getAudioContext]
    );

    const stopPlayback = useCallback(async () => {
        const audioContext = audioContextRef.current;
        audioContextRef.current = null;
        nextStartTimeRef.current = 0;
        if (audioContext && audioContext.state !== "closed") {
            await audioContext.close();
        }
    }, []);

    const resetPlaybackTime = useCallback(() => {
        nextStartTimeRef.current = 0;
    }, []);

    return {
        playAudioChunk,
        stopPlayback,
        resetPlaybackTime
    };
}
