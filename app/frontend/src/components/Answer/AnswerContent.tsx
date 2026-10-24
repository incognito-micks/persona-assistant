import DOMPurify from "dompurify";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";

export default function AnswerContent({ content }: { content: string }) {
    return <ReactMarkdown children={DOMPurify.sanitize(content)} rehypePlugins={[rehypeRaw]} remarkPlugins={[remarkGfm]} />;
}
