import { renderToStaticMarkup } from "react-dom/server";
import { ChatAppResponse } from "../../api";

export type CitationDetail = {
    reference: string;
    index: number;
    isWeb: boolean;
};

type CitationFragment =
    | { type: "text"; value: string }
    | {
          type: "citation";
          detail: CitationDetail;
      };

type HtmlParsedAnswer = {
    answerHtml: string;
    citations: CitationDetail[];
};

const isWebCitation = (reference: string) => reference.startsWith("http://") || reference.startsWith("https://");

const normalizeAnswerText = (answer: ChatAppResponse, isStreaming: boolean): string => {
    let parsedAnswer = answer.message.content.trim();

    if (isStreaming) {
        let lastIndex = parsedAnswer.length;
        for (let i = parsedAnswer.length - 1; i >= 0; i--) {
            if (parsedAnswer[i] === "]") {
                break;
            } else if (parsedAnswer[i] === "[") {
                lastIndex = i;
                break;
            }
        }
        parsedAnswer = parsedAnswer.substring(0, lastIndex);
    }

    return parsedAnswer;
};

const collectCitations = (answer: ChatAppResponse, isStreaming: boolean): { fragments: CitationFragment[]; citations: CitationDetail[] } => {
    const possibleCitations = answer.context.data_points.citations || [];
    const externalResults = answer.context.data_points.external_results_metadata || [];
    const parsedAnswer = normalizeAnswerText(answer, isStreaming);
    const parts = parsedAnswer.split(/\[([^\]]+)\]/g);

    // Helper to resolve SharePoint filename to URL
    const resolveSharePointUrl = (citation: string): string => {
        // If it's already a URL, return as-is
        if (isWebCitation(citation)) {
            return citation;
        }
        // Check if this looks like a filename (has an extension)
        const hasFileExtension = /\.(pdf|docx?|xlsx?|pptx?|txt|html?|csv)$/i.test(citation);
        if (!hasFileExtension) {
            return citation;
        }

        // Look for matching SharePoint URL in external_results_metadata
        // Match by checking if the URL ends with the filename
        const matchingResult = externalResults.find(result => {
            if (!result.url) return false;
            const urlParts = result.url.split("/");
            const urlFilename = urlParts[urlParts.length - 1];
            return urlFilename === citation || decodeURIComponent(urlFilename) === citation;
        });

        return matchingResult?.url || citation;
    };

    const fragments: CitationFragment[] = [];
    const citationMap = new Map<string, CitationDetail>();
    const citationList: CitationDetail[] = [];

    parts.forEach((part, index) => {
        if (index % 2 === 0) {
            fragments.push({ type: "text", value: part });
            return;
        }

        const isValidCitation = possibleCitations.some(citation => citation.endsWith(part));
        if (!isValidCitation) {
            fragments.push({ type: "text", value: `[${part}]` });
            return;
        }

        // Resolve SharePoint filename to URL if applicable
        const resolvedReference = resolveSharePointUrl(part);

        // Check if this resolved reference already exists
        const existing = citationMap.get(resolvedReference);
        if (existing) {
            fragments.push({ type: "citation", detail: existing });
            return;
        }

        const detail: CitationDetail = {
            reference: resolvedReference,
            index: citationList.length + 1,
            isWeb: isWebCitation(resolvedReference)
        };

        citationMap.set(resolvedReference, detail);
        citationList.push(detail);
        fragments.push({ type: "citation", detail });
    });

    return { fragments, citations: citationList };
};

const renderCitation = (detail: CitationDetail) => {
    const supElement = <sup>{detail.index}</sup>;

    if (detail.isWeb) {
        return renderToStaticMarkup(
            <span className="citationBadgeContainer">
                <a className="supContainer" title={detail.reference} href={detail.reference} target="_blank" rel="noopener noreferrer">
                    {supElement}
                </a>
            </span>
        );
    }

    return renderToStaticMarkup(
        <span className="citationBadgeContainer">
            <span className="supContainer" title={detail.reference}>
                {supElement}
            </span>
        </span>
    );
};

export function parseAnswerToHtml(answer: ChatAppResponse, isStreaming: boolean): HtmlParsedAnswer {
    const { fragments, citations } = collectCitations(answer, isStreaming);
    const answerHtml = fragments.map(fragment => (fragment.type === "text" ? fragment.value : renderCitation(fragment.detail))).join("");

    return {
        answerHtml,
        citations
    };
}

export function extractCitationDetails(answer: ChatAppResponse, isStreaming = false): CitationDetail[] {
    return collectCitations(answer, isStreaming).citations;
}
