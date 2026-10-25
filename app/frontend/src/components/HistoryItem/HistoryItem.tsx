import { useTranslation } from "react-i18next";
import styles from "./HistoryItem.module.css";
import { Delete24Regular } from "@fluentui/react-icons";

export interface HistoryData {
    id: string;
    title: string;
    timestamp: number;
}

interface HistoryItemProps {
    item: HistoryData;
    disabled?: boolean;
    isActive?: boolean;
    onSelect: (id: string) => void;
    onDelete: (id: string) => void;
}

export function HistoryItem({ item, disabled, isActive, onSelect, onDelete }: HistoryItemProps) {
    const { t } = useTranslation();

    return (
        <div className={`${styles.historyItem} ${isActive ? styles.active : ""}`}>
            <button
                type="button"
                disabled={disabled}
                onClick={() => onSelect(item.id)}
                className={styles.historyItemButton}
                aria-current={isActive ? "page" : undefined}
                title={item.title}
            >
                <div className={styles.historyItemTitle}>{item.title}</div>
            </button>
            <button
                type="button"
                disabled={disabled}
                onClick={() => onDelete(item.id)}
                className={styles.deleteButton}
                aria-label={t("history.deleteChat", { title: item.title })}
            >
                <Delete24Regular className={styles.deleteIcon} aria-hidden />
            </button>
        </div>
    );
}
