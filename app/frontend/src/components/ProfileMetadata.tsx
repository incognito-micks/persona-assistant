import { Helmet } from "react-helmet-async";
import { useTranslation } from "react-i18next";
import { useAppContext } from "../appContext";
import { getAssistantName } from "../profile";

export const ProfileMetadata = () => {
    const { publicStatus } = useAppContext();
    const { t } = useTranslation();
    const title = getAssistantName(publicStatus);
    const description = publicStatus?.site_description?.trim() || t("siteDescription");
    const image = publicStatus?.social_image_url?.trim() || "/social-preview.png";

    return (
        <Helmet>
            <title>{title}</title>
            <meta name="description" content={description} />
            <meta property="og:title" content={title} />
            <meta property="og:description" content={description} />
            <meta property="og:image" content={image} />
            <meta name="twitter:title" content={title} />
            <meta name="twitter:description" content={description} />
            <meta name="twitter:image" content={image} />
        </Helmet>
    );
};
