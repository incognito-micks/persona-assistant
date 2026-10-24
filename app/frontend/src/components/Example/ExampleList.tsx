import { Example } from "./Example";
import { useTranslation } from "react-i18next";

import styles from "./Example.module.css";

interface Props {
    onExampleClicked: (value: string, index: number) => void;
    questions?: string[];
}

const DEFAULT_ICONS = ["\u{1F4BB}", "\u{1F30D}", "\u{1F3AF}", "\u{1F4BC}"];

export const ExampleList = ({ onExampleClicked, questions }: Props) => {
    const { t } = useTranslation();

    const examples = questions ?? [t("defaultExamples.1"), t("defaultExamples.2"), t("defaultExamples.3"), t("defaultExamples.4")];

    return (
        <ul className={styles.examplesNavList}>
            {examples.map((question, i) => (
                <li key={i}>
                    <Example text={question} value={question} icon={DEFAULT_ICONS[i % DEFAULT_ICONS.length]} onClick={value => onExampleClicked(value, i)} />
                </li>
            ))}
        </ul>
    );
};
