import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

const MARKDOWN_PLUGINS = [remarkGfm, remarkBreaks];

function MarkdownRendererComponent({ markdown }: { markdown: string }) {
  return <ReactMarkdown remarkPlugins={MARKDOWN_PLUGINS}>{markdown}</ReactMarkdown>;
}

export const MarkdownRenderer = memo(MarkdownRendererComponent);
