import { useState } from "react";
import { Person20Regular } from "@fluentui/react-icons";
import { useTranslation } from "react-i18next";

import type { AdminSettings } from "../../api/models";
import { getAssistantName, getAvatarUrl, isSafeProfileImageUrl } from "../../profile";
import styles from "./Admin.module.css";

export function OwnerPreview({ settings }: { settings: AdminSettings }) {
    const { t } = useTranslation();
    const [failedAvatar, setFailedAvatar] = useState<string | null>(null);
    const validAvatar = isSafeProfileImageUrl(settings.avatar_url);
    const avatar = validAvatar ? getAvatarUrl(settings) : undefined;
    const imageUnavailable = avatar !== undefined && failedAvatar === avatar;
    const questions = settings.example_questions.map(question => question.trim()).filter(Boolean);

    return (
        <aside className={styles.preview} aria-labelledby="owner-preview-title">
            <div className={styles.previewHeading}>
                <h2 id="owner-preview-title">{t("admin.preview.title")}</h2>
                <span className={styles.badge}>{t("admin.preview.draft")}</span>
            </div>
            <p className={styles.helpText}>{t("admin.preview.help")}</p>
            <div className={styles.previewCanvas}>
                {avatar && !imageUnavailable ? (
                    <img src={avatar} className={styles.previewAvatar} alt="" onError={() => setFailedAvatar(avatar)} />
                ) : (
                    <span className={styles.previewAvatarPlaceholder}>
                        <Person20Regular aria-hidden />
                    </span>
                )}
                <h3>{getAssistantName(settings)}</h3>
                <p>{settings.welcome_message.trim() || t("chatEmptyStateSubtitle")}</p>
                {settings.vacation_mode && (
                    <p className={styles.previewAvailability}>
                        {settings.vacation_return_date
                            ? t("profile.vacationUntil", { owner: settings.owner_name, date: settings.vacation_return_date })
                            : t("profile.vacation", { owner: settings.owner_name })}
                    </p>
                )}
                <div className={styles.previewQuestions}>
                    {questions.length ? (
                        questions.map((question, index) => (
                            <span key={index} className={styles.previewQuestion}>
                                {question}
                            </span>
                        ))
                    ) : (
                        <p className={styles.helpText}>{t("admin.preview.examplesEmpty")}</p>
                    )}
                </div>
                {(settings.owner_email || settings.calendar_url || settings.linkedin_url) && (
                    <div className={styles.previewContacts}>
                        {settings.owner_email && <span>{t("profile.email")}</span>}
                        {settings.calendar_url && <span>{t("profile.calendar")}</span>}
                        {settings.linkedin_url && <span>{t("profile.linkedin")}</span>}
                    </div>
                )}
            </div>
            {!validAvatar && <p className={styles.previewError}>{t("admin.preview.imageInvalid")}</p>}
            {imageUnavailable && <p className={styles.previewError}>{t("admin.preview.imageUnavailable")}</p>}
        </aside>
    );
}
