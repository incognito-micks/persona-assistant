import { Stack } from "@fluentui/react";
import { animated, useSpring } from "@react-spring/web";
import { useTranslation } from "react-i18next";

import styles from "./Answer.module.css";
import { AnswerIcon } from "./AnswerIcon";

export const AnswerLoading = () => {
    const { t } = useTranslation();
    const animatedStyles = useSpring({
        from: { opacity: 0, transform: "translateY(12px)" },
        to: { opacity: 1, transform: "translateY(0px)" },
        config: { tension: 200, friction: 20 }
    });

    return (
        <animated.div style={{ ...animatedStyles }}>
            <div className={styles.answerWrapper}>
                <AnswerIcon />
                <Stack className={styles.answerContainer} verticalAlign="space-between">
                    <Stack.Item grow>
                        <div className={styles.loadingShimmer}>
                            <p className={styles.answerText}>
                                {t("generatingAnswer")}
                                <span className={styles.loadingdots} />
                            </p>
                        </div>
                    </Stack.Item>
                </Stack>
            </div>
        </animated.div>
    );
};
