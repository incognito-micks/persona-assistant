import { animated, useSpring } from "@react-spring/web";

import styles from "./UserChatMessage.module.css";

interface Props {
    message: string;
    initials?: string;
}

export const UserChatMessage = ({ message, initials }: Props) => {
    const animatedStyles = useSpring({
        from: { opacity: 0, transform: "translateY(12px)" },
        to: { opacity: 1, transform: "translateY(0px)" },
        config: { tension: 200, friction: 20 }
    });

    return (
        <animated.div style={{ ...animatedStyles }}>
            <div className={styles.container}>
                <div className={styles.message}>{message}</div>
                {initials && <div className={styles.avatar}>{initials}</div>}
            </div>
        </animated.div>
    );
};
