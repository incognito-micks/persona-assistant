import styles from "./Example.module.css";

interface Props {
    text: string;
    value: string;
    icon?: string;
    onClick: (value: string) => void;
}

export const Example = ({ text, value, icon, onClick }: Props) => {
    return (
        <button type="button" className={styles.example} onClick={() => onClick(value)}>
            {icon && (
                <span className={styles.exampleIcon} aria-hidden="true">
                    {icon}
                </span>
            )}
            <span className={styles.exampleText}>{text}</span>
        </button>
    );
};
