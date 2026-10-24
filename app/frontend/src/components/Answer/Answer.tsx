import { lazy, Suspense, useMemo } from "react";
import { Stack } from "@fluentui/react";
import { useTranslation } from "react-i18next";
import { animated, useSpring } from "@react-spring/web";

import styles from "./Answer.module.css";
import { ChatAppResponse } from "../../api";
import { parseAnswerToHtml } from "./AnswerParser";
import { AnswerIcon } from "./AnswerIcon";

const AnswerContent = lazy(() => import("./AnswerContent"));

interface Props {
    answer: ChatAppResponse;
    isStreaming: boolean;
    onFollowupQuestionClicked?: (question: string) => void;
    showFollowupQuestions?: boolean;
}

export const Answer = ({ answer, isStreaming, onFollowupQuestionClicked, showFollowupQuestions }: Props) => {
    const followupQuestions = answer.context?.followup_questions;
    const parsedAnswer = useMemo(() => parseAnswerToHtml(answer, isStreaming), [answer, isStreaming]);
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
                        <div className={styles.answerText}>
                            <Suspense fallback={<span role="status">{t("generatingAnswer")}</span>}>
                                <AnswerContent content={parsedAnswer.answerHtml} />
                            </Suspense>
                        </div>
                    </Stack.Item>

                    {!!parsedAnswer.citations.length && (
                        <Stack.Item>
                            <Stack horizontal wrap tokens={{ childrenGap: 5 }}>
                                <span className={styles.citationLearnMore}>{t("citationWithColon")}</span>
                                {parsedAnswer.citations.map(citation => {
                                    const displayIndex = citation.index;
                                    const reference = citation.reference;
                                    if (citation.isWeb) {
                                        const webEntry = answer.context.data_points.external_results_metadata?.find(w => w.url === reference);
                                        const titleOrUrl = webEntry?.title?.trim() ? webEntry.title : reference;
                                        return (
                                            <span key={`${reference}-${displayIndex}`} className={styles.citationEntry}>
                                                <a className={styles.citation} title={reference} href={reference} target="_blank" rel="noopener noreferrer">
                                                    {`${displayIndex}. ${titleOrUrl}`}
                                                </a>
                                            </span>
                                        );
                                    } else {
                                        return (
                                            <span key={`${reference}-${displayIndex}`} className={styles.citationEntry}>
                                                <span className={styles.citation} title={reference}>
                                                    {`${displayIndex}. ${reference}`}
                                                </span>
                                            </span>
                                        );
                                    }
                                })}
                            </Stack>
                        </Stack.Item>
                    )}

                    {!!followupQuestions?.length && showFollowupQuestions && onFollowupQuestionClicked && (
                        <Stack.Item>
                            <Stack
                                horizontal
                                wrap
                                className={`${!!parsedAnswer.citations.length ? styles.followupQuestionsList : ""}`}
                                tokens={{ childrenGap: 6 }}
                            >
                                <span className={styles.followupQuestionLearnMore}>{t("followupQuestions")}</span>
                                {followupQuestions.map((x, i) => {
                                    return (
                                        <a key={i} className={styles.followupQuestion} title={x} onClick={() => onFollowupQuestionClicked(x)}>
                                            {`${x}`}
                                        </a>
                                    );
                                })}
                            </Stack>
                        </Stack.Item>
                    )}
                </Stack>
            </div>
        </animated.div>
    );
};
