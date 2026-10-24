import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const repositoryRoot = fileURLToPath(new URL("../../", import.meta.url));

const escapeMetadata = (value: string): string =>
    value.replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]!);

function profileMetadata(profilePath?: string): Plugin {
    return {
        name: "profile-metadata",
        apply: "serve",
        transformIndexHtml: {
            order: "pre",
            handler(html) {
                let profile: Record<string, unknown> = {};
                try {
                    const parsed: unknown = JSON.parse(readFileSync(resolve(repositoryRoot, profilePath || "profile.json"), "utf-8"));
                    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) profile = parsed as Record<string, unknown>;
                } catch {
                    console.warn("Unable to read the public profile for development metadata; using neutral defaults.");
                }
                const value = (key: string, fallback: string): string => {
                    const entry = profile[key];
                    return typeof entry === "string" && entry.trim() ? entry : fallback;
                };
                return html
                    .replaceAll("__PROFILE_TITLE__", () => escapeMetadata(value("assistant_name", "Persona Assistant")))
                    .replaceAll("__PROFILE_DESCRIPTION__", () => escapeMetadata(value("site_description", "Chat with a personal AI assistant.")))
                    .replaceAll("__PROFILE_IMAGE__", () => escapeMetadata(value("social_image_url", "/social-preview.png")));
            }
        }
    };
}

export default defineConfig(({ mode }) => ({
    plugins: [react(), profileMetadata(process.env.PROFILE_PATH || loadEnv(mode, repositoryRoot, "").PROFILE_PATH)],
    resolve: {
        preserveSymlinks: true
    },
    build: {
        outDir: "../backend/static",
        emptyOutDir: true,
        sourcemap: true,
        rollupOptions: {
            output: {
                manualChunks: id => {
                    // Shared helpers must not pull lazy owner authentication into the public entry point.
                    if (
                        id.includes("commonjsHelpers") ||
                        id.includes("vite/preload-helper") ||
                        id.includes("/node_modules/react/") ||
                        id.includes("/node_modules/react-dom/") ||
                        id.includes("/node_modules/scheduler/")
                    ) {
                        return "react-runtime";
                    } else if (id.includes("@fluentui/react-icons")) {
                        return "fluentui-icons";
                    } else if (id.includes("@fluentui/react")) {
                        return "fluentui-react";
                    } else if (id.includes("@azure/msal-")) {
                        return "authentication";
                    }
                }
            }
        },
        target: "esnext"
    },
    server: {
        proxy: {
            "/auth_setup_admin": "http://localhost:50505",
            "/chat": "http://localhost:50505",
            "/config": "http://localhost:50505",
            "/chat_history": "http://localhost:50505",
            "/api/": "http://localhost:50505",
            "/realtime": { target: "http://localhost:50505", ws: true }
        }
    }
}));
