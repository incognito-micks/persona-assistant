import { useState, useEffect } from "react";
import {
    Pivot,
    PivotItem,
    TextField,
    Toggle,
    PrimaryButton,
    DefaultButton,
    Spinner,
    SpinnerSize,
    MessageBar,
    MessageBarType,
    IconButton
} from "@fluentui/react";
import { useTranslation } from "react-i18next";

import { AdminApiError, getAdminSettings, updateAdminSettings, getAdminAnalytics } from "../../api";
import { AdminSettings, AnalyticsSummary } from "../../api/models";
import { useAppContext } from "../../appContext";
import type { OwnerAuthClient } from "./adminAuth";
import styles from "./Admin.module.css";

async function getAdminMsalInstance(): Promise<OwnerAuthClient | null> {
    const response = await fetch("/auth_setup_admin");
    if (!response.ok) throw new Error("Failed to fetch admin auth config");
    const config = await response.json();
    if (config.enabled !== true) return null;
    if (!config.msalConfig?.auth?.clientId || !config.msalConfig?.auth?.authority) throw new Error("Incomplete owner auth config");
    const { default: createOwnerAuth } = await import("./adminAuth");
    return createOwnerAuth(config.msalConfig);
}

function Admin() {
    const { t } = useTranslation();
    const { refreshPublicStatus } = useAppContext();
    const [msalInstance, setMsalInstance] = useState<OwnerAuthClient | null>(null);
    const [adminEnabled, setAdminEnabled] = useState<boolean | null>(null);
    const [isAuthenticated, setIsAuthenticated] = useState(false);
    const [authLoading, setAuthLoading] = useState(true);
    const [settings, setSettings] = useState<AdminSettings | null>(null);
    const [savedSettings, setSavedSettings] = useState<AdminSettings | null>(null);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [success, setSuccess] = useState(false);

    const changedSettings: Partial<AdminSettings> =
        settings && savedSettings
            ? Object.fromEntries(
                  Object.entries(settings).filter(([field, value]) => JSON.stringify(value) !== JSON.stringify(savedSettings[field as keyof AdminSettings]))
              )
            : {};
    const hasChanges = Object.keys(changedSettings).length > 0;

    const [analytics, setAnalytics] = useState<AnalyticsSummary | null>(null);
    const [analyticsLoading, setAnalyticsLoading] = useState(false);
    const [analyticsError, setAnalyticsError] = useState<string | null>(null);

    const adminError = (failure: unknown, fallback: string) => {
        if (failure instanceof AdminApiError) {
            if (failure.status === 403) return t("admin.ownerDenied");
            if (failure.status === 401) return t("admin.authRequired");
            if (failure.status === 409 || failure.status === 412) return t("admin.conflict");
        }
        return t(fallback);
    };

    useEffect(() => {
        let cancelled = false;
        const initialize = async () => {
            try {
                const instance = await getAdminMsalInstance();
                if (cancelled) return;
                setAdminEnabled(instance !== null);
                setMsalInstance(instance);
                if (!instance) return;
                const token = await instance.getCachedToken();
                if (cancelled || !token) return;
                setIsAuthenticated(true);
                const data = await getAdminSettings(token);
                if (!cancelled) {
                    setSettings(data);
                    setSavedSettings(data);
                }
            } catch (failure) {
                if (!cancelled) setError(adminError(failure, "admin.authError"));
            } finally {
                if (!cancelled) setAuthLoading(false);
            }
        };
        void initialize();
        return () => {
            cancelled = true;
        };
    }, []);

    const handleAdminLogin = async () => {
        if (!msalInstance) return;
        try {
            const token = await msalInstance.signIn();
            if (token) {
                setIsAuthenticated(true);
                await loadSettings(token);
            }
        } catch (failure) {
            setError(adminError(failure, "admin.loginError"));
        }
    };

    const handleAdminLogout = async () => {
        if (!msalInstance) return;
        try {
            await msalInstance.signOut();
            setSettings(null);
            setSavedSettings(null);
            setAnalytics(null);
            setIsAuthenticated(false);
            setError(null);
            setSuccess(false);
        } catch {
            setError(t("admin.logoutError"));
        }
    };

    const loadSettings = async (token: string) => {
        try {
            setLoading(true);
            setError(null);
            const data = await getAdminSettings(token);
            setSettings(data);
            setSavedSettings(data);
        } catch (failure) {
            setError(adminError(failure, "admin.loadError"));
        } finally {
            setLoading(false);
        }
    };

    const handleSave = async () => {
        if (!settings || !msalInstance || saving || !hasChanges) return;
        try {
            setSaving(true);
            setError(null);
            setSuccess(false);
            const token = await msalInstance.getToken();
            if (!token) {
                setError(t("admin.authRequired"));
                return;
            }
            const patch = {
                ...changedSettings,
                ...(changedSettings.example_questions
                    ? { example_questions: changedSettings.example_questions.map(question => question.trim()).filter(Boolean) }
                    : {})
            };
            const updated = await updateAdminSettings(patch, token);
            setSettings(updated);
            setSavedSettings(updated);
            setSuccess(true);
            void refreshPublicStatus().catch(() => console.error("Unable to refresh the public profile after saving."));
        } catch (failure) {
            setError(adminError(failure, "admin.saveError"));
        } finally {
            setSaving(false);
        }
    };

    const updateField = <K extends keyof AdminSettings>(field: K, value: AdminSettings[K]) => {
        if (!settings || saving) return;
        setSuccess(false);
        setSettings({ ...settings, [field]: value });
    };

    const addFunFact = () => {
        if (!settings) return;
        updateField("fun_facts", [...settings.fun_facts, ""]);
    };

    const updateFunFact = (index: number, value: string) => {
        if (!settings) return;
        const updated = [...settings.fun_facts];
        updated[index] = value;
        updateField("fun_facts", updated);
    };

    const removeFunFact = (index: number) => {
        if (!settings) return;
        updateField(
            "fun_facts",
            settings.fun_facts.filter((_, factIndex) => factIndex !== index)
        );
    };

    const loadAnalytics = async () => {
        try {
            setAnalyticsLoading(true);
            setAnalyticsError(null);
            if (!msalInstance) return;
            const token = await msalInstance.getToken();
            if (!token) {
                setAnalyticsError(t("admin.authRequired"));
                return;
            }
            const data = await getAdminAnalytics(token);
            setAnalytics(data);
        } catch (failure) {
            setAnalyticsError(adminError(failure, "admin.analyticsError"));
        } finally {
            setAnalyticsLoading(false);
        }
    };

    const formatTimestamp = (ts: number): string => {
        return new Date(ts).toLocaleString();
    };

    if (authLoading) {
        return (
            <div className={styles.adminContainer}>
                <Spinner size={SpinnerSize.large} label={t("admin.initializing")} />
            </div>
        );
    }

    if (!adminEnabled) {
        return (
            <div className={styles.adminContainer}>
                <h2 className={styles.adminTitle}>{t("admin.title")}</h2>
                {error && <MessageBar messageBarType={MessageBarType.error}>{error}</MessageBar>}
                <MessageBar>{t("admin.disabled")}</MessageBar>
                <p>{t("admin.setup")}</p>
            </div>
        );
    }

    if (!isAuthenticated) {
        return (
            <div className={styles.adminContainer}>
                <h2 className={styles.adminTitle}>{t("admin.title")}</h2>
                {error && <MessageBar messageBarType={MessageBarType.error}>{error}</MessageBar>}
                <p>{t("admin.signInHelp")}</p>
                <PrimaryButton text={t("admin.signIn")} onClick={handleAdminLogin} />
            </div>
        );
    }

    if (loading) {
        return (
            <div className={styles.adminContainer}>
                <Spinner size={SpinnerSize.large} label={t("admin.loading")} />
            </div>
        );
    }

    if (!settings) {
        return (
            <div className={styles.adminContainer}>
                <h2 className={styles.adminTitle}>{t("admin.title")}</h2>
                {error && <MessageBar messageBarType={MessageBarType.error}>{error}</MessageBar>}
                <DefaultButton text={t("admin.signOut")} onClick={handleAdminLogout} />
            </div>
        );
    }

    return (
        <div className={styles.adminContainer}>
            <h2 className={styles.adminTitle}>{t("admin.settingsTitle")}</h2>
            <DefaultButton text={t("admin.signOut")} onClick={handleAdminLogout} disabled={saving} />

            {error && <MessageBar messageBarType={MessageBarType.error}>{error}</MessageBar>}
            {success && <MessageBar messageBarType={MessageBarType.success}>{t("admin.saved")}</MessageBar>}

            <Pivot>
                <PivotItem headerText={t("admin.settingsTab")}>
                    <div className={styles.settingsForm}>
                        {(
                            [
                                ["owner_name", "ownerName"],
                                ["assistant_name", "assistantName"],
                                ["site_description", "description"],
                                ["avatar_url", "avatarUrl"],
                                ["social_image_url", "socialImageUrl"],
                                ["owner_email", "ownerEmail"],
                                ["calendar_url", "calendarUrl"],
                                ["linkedin_url", "linkedinUrl"],
                                ["tone", "tone"],
                                ["manager_email", "managerEmail"]
                            ] as const
                        ).map(([field, label]) => (
                            <TextField
                                key={field}
                                disabled={saving}
                                label={t(`admin.fields.${label}`)}
                                value={settings[field] || ""}
                                onChange={(_, value) => updateField(field, value || "")}
                            />
                        ))}

                        <TextField
                            disabled={saving}
                            label={t("admin.fields.welcome")}
                            multiline
                            rows={3}
                            value={settings.welcome_message}
                            onChange={(_, v) => updateField("welcome_message", v || "")}
                        />

                        <TextField
                            disabled={saving}
                            label={t("admin.fields.examples")}
                            description={t("admin.examplesHelp")}
                            multiline
                            rows={4}
                            value={settings.example_questions?.join("\n") || ""}
                            onChange={(_, value) => updateField("example_questions", value ? value.split("\n") : [])}
                        />

                        <Toggle
                            disabled={saving}
                            label={t("admin.fields.vacation")}
                            checked={settings.vacation_mode}
                            onChange={(_, checked) => updateField("vacation_mode", !!checked)}
                        />

                        {settings.vacation_mode && (
                            <TextField
                                disabled={saving}
                                label={t("admin.fields.returnDate")}
                                value={settings.vacation_return_date || ""}
                                onChange={(_, v) => updateField("vacation_return_date", v || null)}
                            />
                        )}

                        <div className={styles.fieldGroup}>
                            <span className={styles.fieldLabel}>{t("admin.fields.funFacts")}</span>
                            {settings.fun_facts.map((fact, i) => (
                                <div key={i} className={styles.funFactItem}>
                                    <TextField disabled={saving} className={styles.funFactInput} value={fact} onChange={(_, v) => updateFunFact(i, v || "")} />
                                    <IconButton
                                        disabled={saving}
                                        iconProps={{ iconName: "Delete" }}
                                        onClick={() => removeFunFact(i)}
                                        ariaLabel={t("admin.removeFunFact")}
                                    />
                                </div>
                            ))}
                            <DefaultButton disabled={saving} text={t("admin.addFunFact")} onClick={addFunFact} iconProps={{ iconName: "Add" }} />
                        </div>

                        <div className={styles.buttonRow}>
                            <PrimaryButton text={t(saving ? "admin.saving" : "admin.save")} onClick={handleSave} disabled={saving || !hasChanges} />
                        </div>
                    </div>
                </PivotItem>

                <PivotItem headerText={t("admin.analyticsTab")}>
                    <div className={styles.analyticsTab}>
                        <div className={styles.analyticsHeader}>
                            <h3>{t("admin.analyticsTitle")}</h3>
                            <DefaultButton text={t("admin.refresh")} iconProps={{ iconName: "Refresh" }} onClick={loadAnalytics} disabled={analyticsLoading} />
                        </div>
                        <p>{t("admin.analyticsPrivacy")}</p>

                        {analyticsError && <MessageBar messageBarType={MessageBarType.error}>{analyticsError}</MessageBar>}

                        {analyticsLoading ? (
                            <Spinner size={SpinnerSize.medium} label={t("admin.analyticsLoading")} />
                        ) : !analytics ? (
                            <div className={styles.emptyState}>
                                <p>{t("admin.analyticsEmpty")}</p>
                                <DefaultButton text={t("admin.loadAnalytics")} onClick={loadAnalytics} />
                            </div>
                        ) : (
                            <div className={styles.analyticsContent}>
                                <div className={styles.statsGrid}>
                                    <div className={styles.statCard}>
                                        <div className={styles.statValue}>{analytics.total_queries}</div>
                                        <div className={styles.statLabel}>{t("admin.metrics.queries")}</div>
                                    </div>
                                    <div className={styles.statCard}>
                                        <div className={styles.statValue}>{analytics.unique_users}</div>
                                        <div className={styles.statLabel}>{t("admin.metrics.sessions")}</div>
                                    </div>
                                    <div className={styles.statCard}>
                                        <div className={styles.statValue}>{analytics.avg_response_time_ms}ms</div>
                                        <div className={styles.statLabel}>{t("admin.metrics.responseTime")}</div>
                                    </div>
                                    <div className={styles.statCard}>
                                        <div className={styles.statValue}>{analytics.avg_session_depth}</div>
                                        <div className={styles.statLabel}>{t("admin.metrics.sessionDepth")}</div>
                                    </div>
                                    <div className={styles.statCard}>
                                        <div className={styles.statValue}>{analytics.example_question_rate}%</div>
                                        <div className={styles.statLabel}>{t("admin.metrics.examples")}</div>
                                    </div>
                                    <div className={styles.statCard}>
                                        <div className={styles.statValue}>{analytics.confidence_distribution.high}</div>
                                        <div className={styles.statLabel}>{t("admin.metrics.highConfidence")}</div>
                                    </div>
                                    <div className={styles.statCard}>
                                        <div className={styles.statValue}>{analytics.confidence_distribution.low}</div>
                                        <div className={styles.statLabel}>{t("admin.metrics.lowConfidence")}</div>
                                    </div>
                                    <div className={styles.statCard}>
                                        <div className={styles.statValue}>{analytics.error_count}</div>
                                        <div className={styles.statLabel}>{t("admin.metrics.errors")}</div>
                                    </div>
                                </div>

                                <div className={styles.confidenceBar}>
                                    <h4>{t("admin.confidenceTitle")}</h4>
                                    <div className={styles.barContainer}>
                                        {analytics.total_queries > 0 && (
                                            <>
                                                <div
                                                    className={styles.barSegmentHigh}
                                                    style={{ width: `${(analytics.confidence_distribution.high / analytics.total_queries) * 100}%` }}
                                                    title={`${t("admin.confidence.high")}: ${analytics.confidence_distribution.high}`}
                                                />
                                                <div
                                                    className={styles.barSegmentMedium}
                                                    style={{ width: `${(analytics.confidence_distribution.medium / analytics.total_queries) * 100}%` }}
                                                    title={`${t("admin.confidence.medium")}: ${analytics.confidence_distribution.medium}`}
                                                />
                                                <div
                                                    className={styles.barSegmentLow}
                                                    style={{ width: `${(analytics.confidence_distribution.low / analytics.total_queries) * 100}%` }}
                                                    title={`${t("admin.confidence.low")}: ${analytics.confidence_distribution.low}`}
                                                />
                                                <div
                                                    className={styles.barSegmentUnknown}
                                                    style={{ width: `${(analytics.confidence_distribution.unknown / analytics.total_queries) * 100}%` }}
                                                    title={`${t("admin.confidence.unknown")}: ${analytics.confidence_distribution.unknown}`}
                                                />
                                            </>
                                        )}
                                    </div>
                                    <div className={styles.barLegend}>
                                        <span className={styles.legendItem}>
                                            <span className={styles.legendDotHigh} /> {t("admin.confidence.high")} ({analytics.confidence_distribution.high})
                                        </span>
                                        <span className={styles.legendItem}>
                                            <span className={styles.legendDotMedium} /> {t("admin.confidence.medium")} (
                                            {analytics.confidence_distribution.medium})
                                        </span>
                                        <span className={styles.legendItem}>
                                            <span className={styles.legendDotLow} /> {t("admin.confidence.low")} ({analytics.confidence_distribution.low})
                                        </span>
                                        <span className={styles.legendItem}>
                                            <span className={styles.legendDotUnknown} /> {t("admin.confidence.unknown")} (
                                            {analytics.confidence_distribution.unknown})
                                        </span>
                                    </div>
                                </div>

                                {analytics.recent_queries.length > 0 && (
                                    <div className={styles.recentQueries}>
                                        <h4>{t("admin.recentActivity")}</h4>
                                        <div className={styles.queryList}>
                                            {analytics.recent_queries.map((q, i) => (
                                                <div key={i} className={styles.queryItem}>
                                                    <div className={styles.queryText}>{t("admin.chatRequest")}</div>
                                                    <div className={styles.queryMeta}>
                                                        <span className={`${styles.confidenceTag} ${styles[`confidence_${q.confidence_level}`]}`}>
                                                            {t(`admin.confidence.${q.confidence_level}`, { defaultValue: t("admin.confidence.unknown") })}
                                                        </span>
                                                        <span>{q.response_time_ms}ms</span>
                                                        <span>{formatTimestamp(q.timestamp)}</span>
                                                        {q.error && <span className={styles.queryError}>{t("admin.requestFailed")}</span>}
                                                    </div>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                </PivotItem>
            </Pivot>
        </div>
    );
}

export const Component = Admin;
