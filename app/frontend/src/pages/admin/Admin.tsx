import { useState, useEffect, useRef, type ReactNode } from "react";
import { useBlocker } from "react-router-dom";
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
    IconButton,
    Dialog,
    DialogFooter,
    DialogType
} from "@fluentui/react";
import {
    Add20Regular,
    ArrowExit20Regular,
    ArrowSync20Regular,
    Checkmark20Regular,
    Delete20Regular,
    Edit20Regular,
    Info20Regular,
    LockClosed20Regular,
    Person20Regular,
    Save20Regular
} from "@fluentui/react-icons";
import { useTranslation } from "react-i18next";

import { AdminApiError, getAdminSettings, updateAdminSettings, getAdminAnalytics } from "../../api";
import { AdminSettings, AnalyticsSummary } from "../../api/models";
import { useAppContext } from "../../appContext";
import { getAssistantName, getAvatarUrl } from "../../profile";
import type { OwnerAuthClient } from "./adminAuth";
import { OwnerPreview } from "./OwnerPreview";
import styles from "./Admin.module.css";

async function getAdminMsalInstance(): Promise<OwnerAuthClient | null> {
    const response = await fetch("/auth_setup_admin");
    if (!response.ok) throw new Error("Failed to fetch admin auth config");
    const config = await response.json();
    if (typeof config?.enabled !== "boolean") throw new Error("Invalid owner auth configuration");
    if (!config.enabled) return null;
    if (!config.msalConfig?.auth?.clientId || !config.msalConfig?.auth?.authority) throw new Error("Incomplete owner auth config");
    const { default: createOwnerAuth } = await import("./adminAuth");
    return createOwnerAuth(config.msalConfig);
}

function OwnerAccessCard({ children }: { children: ReactNode }) {
    const { t } = useTranslation();
    const { publicStatus } = useAppContext();

    return (
        <section className={styles.authContainer} aria-labelledby="owner-access-title">
            <div className={styles.authCard}>
                <div className={styles.authBrand}>
                    <img src={getAvatarUrl(publicStatus)} alt="" className={styles.authAvatar} />
                    <span>{getAssistantName(publicStatus)}</span>
                </div>
                <p className={styles.eyebrow}>{t("admin.ownerLink")}</p>
                <h1 id="owner-access-title">{t("admin.title")}</h1>
                <p className={styles.authIntro}>{t("admin.signInHelp")}</p>
                {children}
                <p className={styles.ownerOnly}>
                    <LockClosed20Regular aria-hidden />
                    {t("admin.ownerOnly")}
                </p>
            </div>
        </section>
    );
}

function Admin() {
    const { t } = useTranslation();
    const { refreshPublicStatus } = useAppContext();
    const [msalInstance, setMsalInstance] = useState<OwnerAuthClient | null>(null);
    const [adminEnabled, setAdminEnabled] = useState<boolean | null>(null);
    const [isAuthenticated, setIsAuthenticated] = useState(false);
    const [authLoading, setAuthLoading] = useState(true);
    const [authAttempt, setAuthAttempt] = useState(0);
    const [signingIn, setSigningIn] = useState(false);
    const [signingOut, setSigningOut] = useState(false);
    const [accountLabel, setAccountLabel] = useState<string | null>(null);
    const [ownerDenied, setOwnerDenied] = useState(false);
    const [authNotice, setAuthNotice] = useState<string | null>(null);
    const [settings, setSettings] = useState<AdminSettings | null>(null);
    const [savedSettings, setSavedSettings] = useState<AdminSettings | null>(null);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [success, setSuccess] = useState(false);
    const [saveWarning, setSaveWarning] = useState(false);
    const [conflict, setConflict] = useState(false);
    const [pendingAction, setPendingAction] = useState<"signOut" | "reload" | null>(null);

    const changedSettings: Partial<AdminSettings> =
        settings && savedSettings
            ? Object.fromEntries(
                  Object.entries(settings).filter(([field, value]) => JSON.stringify(value) !== JSON.stringify(savedSettings[field as keyof AdminSettings]))
              )
            : {};
    const hasChanges = Object.keys(changedSettings).length > 0;
    const blocker = useBlocker(({ currentLocation, nextLocation }) => (hasChanges || saving) && currentLocation.pathname !== nextLocation.pathname);

    const [analytics, setAnalytics] = useState<AnalyticsSummary | null>(null);
    const [analyticsLoading, setAnalyticsLoading] = useState(false);
    const [analyticsError, setAnalyticsError] = useState<string | null>(null);
    const [activeTab, setActiveTab] = useState("settings");
    const usageHeading = useRef<HTMLHeadingElement | null>(null);
    const usageRequest = useRef<AbortController | null>(null);

    useEffect(() => {
        return () => {
            usageRequest.current?.abort();
            usageRequest.current = null;
        };
    }, []);

    const adminError = (failure: unknown, fallback: string) => {
        if (typeof failure === "object" && failure !== null && "errorCode" in failure) {
            const code = failure.errorCode;
            if (code === "user_cancelled") return t("admin.signInCanceled");
            if (code === "interaction_in_progress") return t("admin.signInInProgress");
            if (code === "popup_window_error" || code === "empty_window_error") return t("admin.signInPopupBlocked");
            if (typeof code === "string" && /^[a-z0-9_]{1,80}$/.test(code)) return t("admin.signInDiagnostic", { code });
        }
        if (failure instanceof AdminApiError) {
            if (failure.status === 403) return t("admin.ownerDenied");
            if (failure.status === 401) return t("admin.authRequired");
            if (failure.status === 409 || failure.status === 412) return t("admin.conflict");
        }
        return t(fallback);
    };

    const updateAccountLabel = (instance: OwnerAuthClient) => {
        const account = instance.getAccount();
        setAccountLabel(account?.username || account?.name || null);
    };

    useEffect(() => {
        if (!hasChanges && !saving) return;
        const beforeUnload = (event: BeforeUnloadEvent) => {
            event.preventDefault();
            event.returnValue = "";
        };
        window.addEventListener("beforeunload", beforeUnload);
        return () => window.removeEventListener("beforeunload", beforeUnload);
    }, [hasChanges, saving]);

    useEffect(() => {
        if (blocker.state === "blocked" && !hasChanges && !saving) blocker.proceed();
    }, [blocker, hasChanges, saving]);

    useEffect(() => {
        let cancelled = false;
        const initialize = async () => {
            try {
                const instance = await getAdminMsalInstance();
                if (cancelled) return;
                setAdminEnabled(instance !== null);
                setMsalInstance(instance);
                if (!instance) return;
                updateAccountLabel(instance);
                const token = await instance.getCachedToken();
                if (cancelled || !token) return;
                const data = await getAdminSettings(token);
                if (!cancelled) {
                    setSettings(data);
                    setSavedSettings(data);
                    setIsAuthenticated(true);
                }
            } catch (failure) {
                if (!cancelled) {
                    setError(adminError(failure, "admin.authError"));
                    setOwnerDenied(failure instanceof AdminApiError && failure.status === 403);
                }
            } finally {
                if (!cancelled) setAuthLoading(false);
            }
        };
        void initialize();
        return () => {
            cancelled = true;
        };
    }, [authAttempt]);

    const handleAdminLogin = async () => {
        if (!msalInstance || signingIn) return;
        setSigningIn(true);
        setError(null);
        setAuthNotice(null);
        setOwnerDenied(false);
        try {
            const token = await msalInstance.signIn();
            updateAccountLabel(msalInstance);
            await loadSettings(token);
        } catch (failure) {
            if (typeof failure === "object" && failure !== null && "errorCode" in failure && failure.errorCode === "user_cancelled") {
                setAuthNotice(t("admin.signInCanceled"));
            } else {
                setError(adminError(failure, "admin.loginError"));
            }
        } finally {
            setSigningIn(false);
        }
    };

    const handleAdminLogout = async () => {
        if (!msalInstance || signingOut) return;
        setSigningOut(true);
        try {
            await msalInstance.signOut();
            usageRequest.current?.abort();
            usageRequest.current = null;
            setSettings(null);
            setSavedSettings(null);
            setAnalytics(null);
            setAnalyticsLoading(false);
            setAnalyticsError(null);
            setActiveTab("settings");
            setIsAuthenticated(false);
            setError(null);
            setSuccess(false);
            setSaveWarning(false);
            setConflict(false);
            setAccountLabel(null);
            setOwnerDenied(false);
            setAuthNotice(null);
        } catch {
            setError(t("admin.logoutError"));
        } finally {
            setSigningOut(false);
        }
    };

    const loadSettings = async (token: string): Promise<boolean> => {
        try {
            setLoading(true);
            setError(null);
            const data = await getAdminSettings(token);
            setSettings(data);
            setSavedSettings(data);
            setIsAuthenticated(true);
            setOwnerDenied(false);
            return true;
        } catch (failure) {
            setError(adminError(failure, "admin.loadError"));
            setOwnerDenied(failure instanceof AdminApiError && failure.status === 403);
            setIsAuthenticated(false);
            return false;
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
            setSaveWarning(false);
            setConflict(false);
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
            if (activeTab === "usage") usageHeading.current?.focus();
            try {
                await refreshPublicStatus();
            } catch {
                console.error("Unable to refresh the public profile after saving.");
                setSaveWarning(true);
            }
        } catch (failure) {
            setError(adminError(failure, "admin.saveError"));
            setConflict(failure instanceof AdminApiError && (failure.status === 409 || failure.status === 412));
        } finally {
            setSaving(false);
        }
    };

    const updateField = <K extends keyof AdminSettings>(field: K, value: AdminSettings[K]) => {
        if (!settings || saving || signingOut) return;
        setSuccess(false);
        setSaveWarning(false);
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
        usageRequest.current?.abort();
        const controller = new AbortController();
        usageRequest.current = controller;
        try {
            setAnalyticsLoading(true);
            setAnalyticsError(null);
            const token = await msalInstance?.getToken();
            if (controller.signal.aborted) return;
            if (!token) {
                setAnalytics(null);
                setAnalyticsError(t("admin.authRequired"));
                return;
            }
            const data = await getAdminAnalytics(token, controller.signal);
            if (controller.signal.aborted) return;
            setAnalytics(data);
        } catch (failure) {
            if (controller.signal.aborted) return;
            setAnalytics(null);
            setAnalyticsError(
                failure instanceof AdminApiError && failure.status === 503 ? t("admin.analyticsNotConfigured") : adminError(failure, "admin.analyticsError")
            );
        } finally {
            if (usageRequest.current === controller) {
                usageRequest.current = null;
                setAnalyticsLoading(false);
            }
        }
    };

    const retryInitialization = () => {
        setAuthLoading(true);
        setAdminEnabled(null);
        setError(null);
        setAuthNotice(null);
        setOwnerDenied(false);
        setAuthAttempt(attempt => attempt + 1);
    };

    const resetSignIn = async () => {
        if (!msalInstance || authLoading || signingIn || loading) return;
        setAuthLoading(true);
        setError(null);
        setAuthNotice(null);
        try {
            await msalInstance.resetSignIn();
            setAccountLabel(null);
            retryInitialization();
        } catch {
            console.error("Unable to reset Microsoft owner sign-in.");
            setError(t("admin.resetSignInError"));
            setAuthLoading(false);
        }
    };

    const performAction = async (action: "signOut" | "reload") => {
        setPendingAction(null);
        if (action === "signOut") {
            await handleAdminLogout();
        } else if (msalInstance) {
            try {
                const token = await msalInstance.getToken();
                if (!token) {
                    setError(t("admin.authRequired"));
                    return;
                }
                if (!(await loadSettings(token))) return;
                setConflict(false);
                setSuccess(false);
                try {
                    await refreshPublicStatus();
                } catch {
                    console.error("Unable to refresh the public profile after reloading.");
                    setSaveWarning(true);
                }
            } catch (failure) {
                setError(adminError(failure, "admin.loadError"));
            }
        }
    };

    const requestAction = (action: "signOut" | "reload") => {
        if (saving || signingOut) return;
        if (hasChanges) setPendingAction(action);
        else void performAction(action);
    };

    const keepEditing = () => {
        setPendingAction(null);
        if (blocker.state === "blocked") blocker.reset();
    };

    const discardAndContinue = () => {
        if (saving) return;
        setSettings(savedSettings);
        setSuccess(false);
        if (blocker.state === "blocked") blocker.proceed();
        else if (pendingAction) void performAction(pendingAction);
    };

    const leaveDialog = (
        <Dialog
            hidden={blocker.state !== "blocked" && pendingAction === null}
            onDismiss={keepEditing}
            dialogContentProps={{
                type: DialogType.normal,
                showCloseButton: false,
                title: t(saving ? "admin.leaveSavingTitle" : "admin.leaveTitle"),
                subText: t(saving ? "admin.leaveSavingHelp" : "admin.leaveHelp")
            }}
            modalProps={{ isBlocking: false, className: styles.ownerDialog }}
        >
            <DialogFooter>
                <DefaultButton text={t("admin.leaveStay")} onClick={keepEditing} />
                {!saving && <PrimaryButton className={styles.primaryButton} text={t("admin.leaveDiscard")} onClick={discardAndContinue} />}
            </DialogFooter>
        </Dialog>
    );

    const formatTimestamp = (ts: number): string => {
        return new Date(ts).toLocaleString();
    };

    const formatUsageDate = (date: string): string =>
        new Date(`${date}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });

    if (authLoading) {
        return (
            <OwnerAccessCard>
                <Spinner size={SpinnerSize.large} label={t("admin.initializing")} />
            </OwnerAccessCard>
        );
    }

    if (adminEnabled === false) {
        return (
            <OwnerAccessCard>
                <MessageBar>{t("admin.disabled")}</MessageBar>
                <details className={styles.setupHelp}>
                    <summary>{t("admin.setupHelp")}</summary>
                    <p>{t("admin.setup")}</p>
                </details>
                <DefaultButton text={t("admin.retry")} onClick={retryInitialization} />
            </OwnerAccessCard>
        );
    }

    if (adminEnabled === null) {
        return (
            <OwnerAccessCard>
                <MessageBar messageBarType={MessageBarType.error}>{error}</MessageBar>
                <DefaultButton text={t("admin.retry")} onClick={retryInitialization} />
            </OwnerAccessCard>
        );
    }

    if (!isAuthenticated) {
        return (
            <OwnerAccessCard>
                {accountLabel && (
                    <p className={styles.accountLabel}>
                        {t("admin.microsoftAccount")}: {accountLabel}
                    </p>
                )}
                {error && <MessageBar messageBarType={MessageBarType.error}>{error}</MessageBar>}
                {authNotice && <MessageBar>{authNotice}</MessageBar>}
                <PrimaryButton
                    className={styles.primaryButton}
                    text={t(signingIn || loading ? "admin.signingIn" : ownerDenied ? "admin.switchAccount" : "admin.signIn")}
                    onClick={handleAdminLogin}
                    disabled={signingIn || loading}
                />
                {(signingIn || loading) && <Spinner size={SpinnerSize.small} label={t(loading ? "admin.loading" : "admin.signingIn")} />}
                {error && !ownerDenied && !signingIn && (
                    <>
                        <DefaultButton text={t("admin.resetSignIn")} onClick={resetSignIn} />
                        <p className={styles.authIntro}>{t("admin.resetSignInHelp")}</p>
                    </>
                )}
            </OwnerAccessCard>
        );
    }

    if (loading) {
        return (
            <OwnerAccessCard>
                <Spinner size={SpinnerSize.large} label={t("admin.loading")} />
            </OwnerAccessCard>
        );
    }

    if (!settings) {
        return (
            <OwnerAccessCard>
                {error && <MessageBar messageBarType={MessageBarType.error}>{error}</MessageBar>}
                <DefaultButton text={t("admin.signOut")} onClick={handleAdminLogout} />
            </OwnerAccessCard>
        );
    }

    const fieldsDisabled = saving || signingOut;
    const maxDailyQueries = Math.max(1, ...(analytics?.queries_by_day.map(day => day.count) ?? []));

    return (
        <div className={styles.adminContainer}>
            <header className={styles.workspaceHeader}>
                <div>
                    <p className={styles.eyebrow}>{getAssistantName(settings)}</p>
                    <h1 className={styles.adminTitle}>{t("admin.settingsTitle")}</h1>
                    <p className={styles.workspaceIntro}>{t("admin.workspaceIntro")}</p>
                </div>
                <div className={styles.accountControls}>
                    <span className={styles.accountLabel}>
                        <Person20Regular aria-hidden />
                        {t("admin.signedInAs", { account: accountLabel || t("admin.microsoftAccount") })}
                    </span>
                    <DefaultButton
                        text={t(signingOut ? "admin.signingOut" : "admin.signOut")}
                        onClick={() => requestAction("signOut")}
                        disabled={fieldsDisabled}
                        onRenderIcon={() => <ArrowExit20Regular aria-hidden className={styles.buttonIcon} />}
                    />
                </div>
            </header>

            {error && (
                <MessageBar
                    messageBarType={MessageBarType.error}
                    actions={
                        conflict ? (
                            <DefaultButton text={t("admin.reloadLatest")} onClick={() => requestAction("reload")} disabled={fieldsDisabled} />
                        ) : undefined
                    }
                >
                    {error}
                </MessageBar>
            )}
            {success && <MessageBar messageBarType={MessageBarType.success}>{t("admin.saved")}</MessageBar>}
            {saveWarning && <MessageBar messageBarType={MessageBarType.warning}>{t("admin.publicRefreshError")}</MessageBar>}

            <Pivot
                aria-label={t("admin.settingsTitle")}
                selectedKey={activeTab}
                onLinkClick={item => {
                    if (item?.props.itemKey) setActiveTab(item.props.itemKey);
                    if (item?.props.itemKey === "usage" && !analytics && !analyticsLoading) void loadAnalytics();
                }}
            >
                <PivotItem itemKey="settings" headerText={t("admin.settingsTab")}>
                    <div className={styles.editorGrid}>
                        <div className={styles.settingsForm}>
                            <section className={styles.settingsSection} aria-labelledby="owner-identity-title">
                                <div className={styles.sectionHeading}>
                                    <h2 id="owner-identity-title">{t("admin.sections.identity")}</h2>
                                    <span className={styles.badge}>{t("admin.publicBadge")}</span>
                                </div>
                                <p className={styles.helpText}>{t("admin.sections.identityHelp")}</p>
                                <div className={styles.fieldGrid}>
                                    {(
                                        [
                                            ["owner_name", "ownerName"],
                                            ["assistant_name", "assistantName"],
                                            ["site_description", "description"],
                                            ["avatar_url", "avatarUrl"],
                                            ["social_image_url", "socialImageUrl"]
                                        ] as const
                                    ).map(([field, label]) => (
                                        <TextField
                                            key={field}
                                            disabled={fieldsDisabled}
                                            label={t(`admin.fields.${label}`)}
                                            description={field === "avatar_url" || field === "social_image_url" ? t("admin.imageHelp") : undefined}
                                            value={settings[field]}
                                            onChange={(_, value) => updateField(field, value || "")}
                                        />
                                    ))}
                                </div>
                            </section>

                            <section className={styles.settingsSection} aria-labelledby="owner-conversation-title">
                                <div className={styles.sectionHeading}>
                                    <h2 id="owner-conversation-title">{t("admin.sections.conversation")}</h2>
                                    <span className={styles.badge}>{t("admin.contextBadge")}</span>
                                </div>
                                <p className={styles.helpText}>{t("admin.sections.conversationHelp")}</p>
                                <TextField
                                    disabled={fieldsDisabled}
                                    label={t("admin.fields.tone")}
                                    multiline
                                    rows={2}
                                    value={settings.tone}
                                    onChange={(_, value) => updateField("tone", value || "")}
                                />
                                <TextField
                                    disabled={fieldsDisabled}
                                    label={t("admin.fields.welcome")}
                                    multiline
                                    rows={3}
                                    value={settings.welcome_message}
                                    onChange={(_, value) => updateField("welcome_message", value || "")}
                                />
                                <TextField
                                    disabled={fieldsDisabled}
                                    label={t("admin.fields.examples")}
                                    description={t("admin.examplesHelp")}
                                    multiline
                                    rows={4}
                                    value={settings.example_questions.join("\n")}
                                    onChange={(_, value) => updateField("example_questions", value ? value.split("\n") : [])}
                                />
                                <div className={styles.fieldGroup}>
                                    <span className={styles.fieldLabel}>{t("admin.fields.funFacts")}</span>
                                    {settings.fun_facts.map((fact, index) => (
                                        <div key={index} className={styles.funFactItem}>
                                            <TextField
                                                disabled={fieldsDisabled}
                                                className={styles.funFactInput}
                                                ariaLabel={t("admin.factInput", { number: index + 1 })}
                                                value={fact}
                                                onChange={(_, value) => updateFunFact(index, value || "")}
                                            />
                                            <IconButton
                                                className={styles.iconButton}
                                                disabled={fieldsDisabled}
                                                onRenderIcon={() => <Delete20Regular aria-hidden />}
                                                onClick={() => removeFunFact(index)}
                                                ariaLabel={t("admin.removeFunFact", { number: index + 1 })}
                                            />
                                        </div>
                                    ))}
                                    <DefaultButton
                                        disabled={fieldsDisabled}
                                        text={t("admin.addFunFact")}
                                        onClick={addFunFact}
                                        onRenderIcon={() => <Add20Regular aria-hidden className={styles.buttonIcon} />}
                                    />
                                </div>
                                <p className={styles.contextNote}>
                                    <Info20Regular aria-hidden />
                                    {t("admin.privacyHelp")}
                                </p>
                            </section>

                            <section className={styles.settingsSection} aria-labelledby="owner-contact-title">
                                <div className={styles.sectionHeading}>
                                    <h2 id="owner-contact-title">{t("admin.sections.contact")}</h2>
                                    <span className={styles.badge}>{t("admin.publicBadge")}</span>
                                </div>
                                <p className={styles.helpText}>{t("admin.sections.contactHelp")}</p>
                                <div className={styles.fieldGrid}>
                                    {(
                                        [
                                            ["owner_email", "ownerEmail"],
                                            ["calendar_url", "calendarUrl"],
                                            ["mentorship_url", "mentorshipUrl"],
                                            ["linkedin_url", "linkedinUrl"]
                                        ] as const
                                    ).map(([field, label]) => (
                                        <TextField
                                            key={field}
                                            disabled={fieldsDisabled}
                                            label={t(`admin.fields.${label}`)}
                                            description={field === "mentorship_url" ? t("admin.mentorshipHelp") : undefined}
                                            value={settings[field]}
                                            onChange={(_, value) => updateField(field, value || "")}
                                        />
                                    ))}
                                </div>
                                <Toggle
                                    disabled={fieldsDisabled}
                                    label={t("admin.fields.vacation")}
                                    checked={settings.vacation_mode}
                                    onChange={(_, checked) => updateField("vacation_mode", !!checked)}
                                />
                                {settings.vacation_mode && (
                                    <TextField
                                        disabled={fieldsDisabled}
                                        type="date"
                                        label={t("admin.fields.returnDate")}
                                        description={t("admin.returnDateHelp")}
                                        value={settings.vacation_return_date || ""}
                                        onChange={(_, value) => updateField("vacation_return_date", value || null)}
                                    />
                                )}
                                <TextField
                                    disabled={fieldsDisabled}
                                    label={t("admin.fields.managerEmail")}
                                    description={t("admin.managerEmailHelp")}
                                    value={settings.manager_email}
                                    onChange={(_, value) => updateField("manager_email", value || "")}
                                />
                            </section>
                        </div>
                        <OwnerPreview settings={settings} />
                    </div>
                </PivotItem>

                <PivotItem itemKey="usage" headerText={t("admin.analyticsTab")}>
                    <div className={styles.analyticsTab}>
                        <div className={styles.analyticsHeader}>
                            <h2 ref={usageHeading} tabIndex={-1}>
                                {t("admin.analyticsTitle")}
                            </h2>
                            <DefaultButton
                                text={t("admin.refresh")}
                                onRenderIcon={() => <ArrowSync20Regular aria-hidden className={styles.buttonIcon} />}
                                onClick={loadAnalytics}
                                disabled={analyticsLoading}
                            />
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
                                {analytics.total_queries === 0 && <p role="status">{t("admin.analyticsNoActivity")}</p>}
                                <div className={styles.statsGrid}>
                                    <div className={styles.statCard}>
                                        <div className={styles.statValue}>{analytics.total_queries}</div>
                                        <div className={styles.statLabel}>{t("admin.metrics.queries")}</div>
                                    </div>
                                    <div className={styles.statCard}>
                                        <div className={styles.statValue}>{analytics.total_sessions}</div>
                                        <div className={styles.statLabel}>{t("admin.metrics.sessions")}</div>
                                    </div>
                                    <div className={styles.statCard}>
                                        <div className={styles.statValue}>{analytics.total_queries ? `${analytics.avg_response_time_ms}ms` : "-"}</div>
                                        <div className={styles.statLabel}>{t("admin.metrics.responseTime")}</div>
                                    </div>
                                    <div className={styles.statCard}>
                                        <div className={styles.statValue}>{analytics.total_queries ? `${analytics.example_question_rate}%` : "-"}</div>
                                        <div className={styles.statLabel}>{t("admin.metrics.examples")}</div>
                                    </div>
                                    <div className={styles.statCard}>
                                        <div className={styles.statValue}>{analytics.error_count}</div>
                                        <div className={styles.statLabel}>{t("admin.metrics.errors")}</div>
                                    </div>
                                </div>
                                <p className={styles.helpText}>{t("admin.usageResponseTimeHelp")}</p>

                                <section className={styles.dailyActivity} aria-labelledby="usage-daily-title">
                                    <h3 id="usage-daily-title">{t("admin.dailyActivity")}</h3>
                                    <ol className={styles.activityChart} role="list" aria-label={t("admin.dailyActivity")}>
                                        {analytics.queries_by_day.map(day => (
                                            <li
                                                key={day.date}
                                                title={t("admin.dailyCount", { date: formatUsageDate(day.date), count: day.count })}
                                                aria-label={t("admin.dailyCount", { date: formatUsageDate(day.date), count: day.count })}
                                            >
                                                <span
                                                    className={styles.activityBar}
                                                    style={{ height: `${(day.count / maxDailyQueries) * 100}%` }}
                                                    aria-hidden
                                                />
                                            </li>
                                        ))}
                                    </ol>
                                    {analytics.queries_by_day.length > 0 && (
                                        <div className={styles.activityRange} aria-hidden>
                                            <span>{formatUsageDate(analytics.queries_by_day[0].date)}</span>
                                            <span>{formatUsageDate(analytics.queries_by_day[analytics.queries_by_day.length - 1].date)}</span>
                                        </div>
                                    )}
                                </section>

                                {analytics.recent_activity.length > 0 && (
                                    <div className={styles.recentQueries}>
                                        <h3>{t("admin.recentActivity")}</h3>
                                        <div className={styles.queryList}>
                                            {analytics.recent_activity.map((q, i) => (
                                                <div key={i} className={styles.queryItem}>
                                                    <div className={styles.queryText}>{t("admin.chatRequest")}</div>
                                                    <div className={styles.queryMeta}>
                                                        <span className={q.failed ? styles.queryError : undefined}>
                                                            {t(q.failed ? "admin.requestFailed" : "admin.requestSucceeded")}
                                                        </span>
                                                        {q.is_example_question && <span>{t("admin.exampleRequest")}</span>}
                                                        <span>{q.response_time_ms}ms</span>
                                                        <span>{formatTimestamp(q.timestamp)}</span>
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
            <div className={styles.saveBar} hidden={activeTab === "usage" && !hasChanges && !saving} role="region" aria-label={t("admin.saveBarLabel")}>
                <span className={styles.saveStatus} role="status">
                    {hasChanges ? <Edit20Regular aria-hidden /> : <Checkmark20Regular aria-hidden />}
                    <span>{t(saving ? "admin.saving" : hasChanges ? "admin.unsaved" : "admin.allSaved")}</span>
                </span>
                <div className={styles.saveActions}>
                    <DefaultButton
                        text={t("admin.discard")}
                        disabled={fieldsDisabled || !hasChanges}
                        onClick={() => {
                            setSettings(savedSettings);
                            setSuccess(false);
                            setSaveWarning(false);
                            if (!conflict) setError(null);
                            if (activeTab === "usage") usageHeading.current?.focus();
                        }}
                    />
                    <PrimaryButton
                        className={styles.primaryButton}
                        text={t(saving ? "admin.saving" : "admin.save")}
                        onRenderIcon={() => <Save20Regular aria-hidden className={styles.buttonIcon} />}
                        onClick={handleSave}
                        disabled={fieldsDisabled || !hasChanges}
                    />
                </div>
            </div>
            {leaveDialog}
        </div>
    );
}

export const Component = Admin;
