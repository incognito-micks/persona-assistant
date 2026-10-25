import { Outlet, Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Mic20Regular } from "@fluentui/react-icons";
import styles from "./Layout.module.css";

import { useAppContext } from "../../appContext";
import { ProfileMetadata } from "../../components/ProfileMetadata";
import { getAssistantName, getAvatarUrl } from "../../profile";

const Layout = () => {
    const { t } = useTranslation();
    const { publicStatus, config } = useAppContext();
    const { pathname } = useLocation();
    const isOwnerWorkspace = pathname === "/admin" || pathname.startsWith("/admin/");
    const isChat = pathname === "/";
    const isVoice = pathname === "/voice";
    const assistantName = getAssistantName(publicStatus);
    const contacts = [
        publicStatus?.owner_email ? { href: `mailto:${publicStatus.owner_email}`, label: t("profile.email") } : null,
        publicStatus?.calendar_url ? { href: publicStatus.calendar_url, label: t("profile.calendar") } : null,
        publicStatus?.mentorship_url ? { href: publicStatus.mentorship_url, label: t("profile.mentorship") } : null,
        publicStatus?.linkedin_url ? { href: publicStatus.linkedin_url, label: t("profile.linkedin") } : null
    ].filter((contact): contact is { href: string; label: string } => contact !== null);

    return (
        <div className={styles.layout}>
            <ProfileMetadata />
            <header className={styles.header} role={"banner"}>
                <div className={styles.headerContainer}>
                    <Link to="/" className={styles.headerTitleContainer}>
                        <img src={getAvatarUrl(publicStatus)} alt="" className={styles.headerAvatar} />
                        <h3 className={styles.headerTitle}>{assistantName}</h3>
                    </Link>
                    {isOwnerWorkspace ? (
                        <Link to="/" className={styles.headerLink}>
                            {t("admin.backToChat")}
                        </Link>
                    ) : config?.showRealtimeVoice && !isVoice ? (
                        <Link to="/voice" className={styles.voiceLink}>
                            <Mic20Regular aria-hidden />
                            {t("voice.open")}
                        </Link>
                    ) : null}
                </div>
                {publicStatus?.vacation_mode && (
                    <div className={styles.vacationBanner}>
                        {publicStatus.vacation_return_date
                            ? t("profile.vacationUntil", { owner: publicStatus.owner_name || assistantName, date: publicStatus.vacation_return_date })
                            : t("profile.vacation", { owner: publicStatus.owner_name || assistantName })}
                    </div>
                )}
            </header>

            <main className={`${styles.main} ${isChat ? "" : styles.scrollPage}`} id="main-content">
                <Outlet />
            </main>

            {!isOwnerWorkspace && contacts.length > 0 && (
                <footer className={styles.contactFooter}>
                    {contacts.map(contact => (
                        <a key={contact.href} href={contact.href} className={styles.contactLink} target="_blank" rel="noopener noreferrer">
                            {contact.label}
                        </a>
                    ))}
                </footer>
            )}
        </div>
    );
};

export default Layout;
