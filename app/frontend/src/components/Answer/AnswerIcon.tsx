import { useAppContext } from "../../appContext";
import { getAvatarUrl } from "../../profile";
import styles from "./Answer.module.css";

export const AnswerIcon = () => {
    const { publicStatus } = useAppContext();
    return <img src={getAvatarUrl(publicStatus)} alt="" className={styles.avatarImage} aria-hidden="true" />;
};
