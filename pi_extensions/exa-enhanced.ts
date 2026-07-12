import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  type AssistantMessage,
  type AssistantMessageEventStream,
  type Context,
  type Model,
  type SimpleStreamOptions,
  createAssistantMessageEventStream,
} from "@earendil-works/pi-ai";
import { b } from "../baml_exa/baml_client/index.ts";
import { fromMarkdown } from "mdast-util-from-markdown";

const EXA_ENDPOINT = "https://demos.exa.ai/chatbot-demo/api/chat/stream";
const UPSTREAM_MODEL = "google/gemini-2.5-flash";

function contentToText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return String(content ?? "");
  return content
    .map((part: any) => {
      if (part?.type === "text") return part.text || "";
      if (part?.type === "thinking") return part.thinking || "";
      if (part?.type === "toolCall")
        return `[already executed tool call ${part.name}: ${JSON.stringify(part.arguments || {})}]`;
      if (part?.type === "toolResult") return `[tool result: ${contentToText(part.content)}]`;
      return "";
    })
    .filter(Boolean)
    .join("\n");
}

function contextToText(context: Context): string {
  const sections: string[] = [];
  if (context.systemPrompt) sections.push(`SYSTEM:\n${context.systemPrompt}`);
  for (const message of context.messages) {
    const role = String(message.role || "user").toUpperCase();
    const text = contentToText(message.content);
    sections.push(`${role}:\n${text}`);
  }
  return sections.join("\n\n");
}

function renderedPrompt(body: any): string {
  const messages = body?.messages ?? body?.input ?? [];
  if (!Array.isArray(messages)) return JSON.stringify(body);
  return messages
    .map((message: any) => {
      const content = typeof message?.content === "string"
        ? message.content
        : JSON.stringify(message?.content ?? "");
      return `${String(message?.role || "user").toUpperCase()}:\n${content}`;
    })
    .join("\n\n");
}

function exaStreamText(raw: string): string {
  let text = "";
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("data:")) continue;
    const payload = trimmed.slice(5).trim();
    if (!payload || payload === "[DONE]") continue;
    try {
      const event = JSON.parse(payload);
      if (typeof event.content === "string") text += event.content;
    } catch {
      // Ignore keepalives and non-content metadata.
    }
  }
  return text;
}

function stripExaFollowups(text: string): string {
  const tree: any = fromMarkdown(text);
  const ranges: Array<[number, number]> = [];

  function visit(node: any): void {
    if (
      node?.type === "code" &&
      String(node.lang || "").toLowerCase() === "followups" &&
      Number.isInteger(node.position?.start?.offset) &&
      Number.isInteger(node.position?.end?.offset)
    ) {
      ranges.push([node.position.start.offset, node.position.end.offset]);
      return;
    }
    if (Array.isArray(node?.children)) node.children.forEach(visit);
  }

  visit(tree);
  let cleaned = text;
  for (const [start, end] of ranges.sort((a, b) => b[0] - a[0])) {
    cleaned = cleaned.slice(0, start) + cleaned.slice(end);
  }
  return cleaned.trim();
}

async function askExa(prompt: string, signal?: AbortSignal): Promise<string> {
  const response = await fetch(EXA_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal,
    body: JSON.stringify({
      message: prompt,
      history: [],
      exaEnabled: false,
      model: UPSTREAM_MODEL,
      searchType: "instant",
    }),
  });
  const raw = await response.text();
  if (!response.ok) throw new Error(`Exa upstream ${response.status}: ${raw.slice(0, 500)}`);
  const text = exaStreamText(raw);
  if (!text) throw new Error("Exa returned no assistant content");
  return text;
}

function streamExaBaml(
  model: Model<any>,
  context: Context,
  options?: SimpleStreamOptions,
): AssistantMessageEventStream {
  const stream = createAssistantMessageEventStream();

  void (async () => {
    const output: AssistantMessage = {
      role: "assistant",
      content: [],
      api: model.api,
      provider: model.provider,
      model: model.id,
      usage: {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
      },
      stopReason: "stop",
      timestamp: Date.now(),
    };

    try {
      stream.push({ type: "start", partial: output });
      const request = await b.request.NextAction(contextToText(context), {
        env: { OPENAI_API_KEY: "unused" },
      });
      const prompt = renderedPrompt(request.body.json());
      const rawText = await askExa(prompt, options?.signal);
      const modelText = stripExaFollowups(rawText);
      let parsed: any;
      try {
        parsed = b.parse.NextAction(modelText, {
          env: { OPENAI_API_KEY: "unused" },
        });
      } catch (firstParseError) {
        try {
          const repairedText = await askExa(
            `${prompt}\n\nThe previous response below did not match the required ` +
            `output type. Preserve its intended action or answer, but re-emit ` +
            `it using exactly the required structure and nothing else.\n\n` +
            `<rejected_response>\n${modelText}\n</rejected_response>`,
            options?.signal,
          );
          parsed = b.parse.NextAction(repairedText, {
            env: { OPENAI_API_KEY: "unused" },
          });
        } catch {
          parsed = { tool: "final", content: modelText };
        }
      }

      if (parsed.tool === "final") {
        output.content.push({ type: "text", text: parsed.content });
        stream.push({ type: "text_start", contentIndex: 0, partial: output });
        stream.push({ type: "text_delta", contentIndex: 0, delta: parsed.content, partial: output });
        stream.push({ type: "text_end", contentIndex: 0, content: parsed.content, partial: output });
        stream.push({ type: "done", reason: "stop", message: output });
      } else {
        if (!context.tools?.some((tool: any) => tool.name === parsed.tool))
          throw new Error(`BAML selected unavailable tool: ${parsed.tool}`);
        const argumentsWithoutNulls = Object.fromEntries(
          Object.entries(parsed.arguments || {}).filter(([, value]) => value != null),
        );
        const toolCall = {
          type: "toolCall" as const,
          id: `exa-enhanced-${crypto.randomUUID()}`,
          name: parsed.tool,
          arguments: argumentsWithoutNulls,
        };
        output.content.push(toolCall);
        output.stopReason = "toolUse";
        stream.push({ type: "toolcall_start", contentIndex: 0, partial: output });
        stream.push({
          type: "toolcall_delta",
          contentIndex: 0,
          delta: JSON.stringify(argumentsWithoutNulls),
          partial: output,
        });
        stream.push({ type: "toolcall_end", contentIndex: 0, toolCall, partial: output });
        stream.push({ type: "done", reason: "toolUse", message: output });
      }
      stream.end();
    } catch (error) {
      output.stopReason = options?.signal?.aborted ? "aborted" : "error";
      output.errorMessage = error instanceof Error ? error.message : String(error);
      stream.push({ type: "error", reason: output.stopReason, error: output });
      stream.end();
    }
  })();

  return stream;
}

export default function (pi: ExtensionAPI) {
  pi.registerProvider("exa-enhanced", {
    name: "Exa Enhanced",
    baseUrl: EXA_ENDPOINT,
    apiKey: "exa-public",
    authHeader: false,
    api: "exa-enhanced" as any,
    models: [
      {
        id: "google/gemini-2.5-flash",
        name: "Gemini 2.5 Flash (Exa Enhanced)",
        reasoning: false,
        input: ["text"],
        contextWindow: 128000,
        maxTokens: 8192,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      },
    ],
    streamSimple: streamExaBaml,
  });
}
