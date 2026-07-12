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
import TypeBuilder, {
  type FieldType,
} from "../baml_exa/baml_client/type_builder.ts";
import { fromMarkdown } from "mdast-util-from-markdown";

const EXA_ENDPOINT = "https://demos.exa.ai/chatbot-demo/api/chat/stream";
const UPSTREAM_MODEL = "google/gemini-2.5-flash";

function contextToText(context: Context): string {
  const messages = context.messages.map((message) => {
    const content = typeof message.content === "string"
      ? message.content
      : message.content
          .map((part: any) =>
            part.type === "text" ? part.text
            : part.type === "thinking" ? part.thinking
            : part.type === "toolCall"
              ? `[already executed tool call ${part.name}: ${JSON.stringify(part.arguments || {})}]`
              : ""
          )
          .filter(Boolean)
          .join("\n");

    return `${String(message.role || "user").toUpperCase()}:\n${content}`;
  });

  return [
    context.systemPrompt && `SYSTEM:\n${context.systemPrompt}`,
    ...messages,
  ].filter(Boolean).join("\n\n");
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

function safeTypeName(value: string): string {
  const words = value.replace(/[^A-Za-z0-9]+/g, " ").trim().split(/\s+/);
  const name = words.map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join("");
  return name && /^[A-Za-z]/.test(name) ? name : `Tool${name || "Value"}`;
}

function scalarFallback(tb: TypeBuilder): FieldType {
  return tb.union([tb.string(), tb.float(), tb.bool(), tb.null()]);
}

function schemaToBaml(
  tb: TypeBuilder,
  schema: any,
  nameHint: string,
  counter: { value: number },
): FieldType {
  if (!schema || schema === true) return scalarFallback(tb);
  if (schema === false) return tb.null();

  if (Object.hasOwn(schema, "const")) {
    if (typeof schema.const === "string") return tb.literalString(schema.const);
    if (typeof schema.const === "number" && Number.isInteger(schema.const))
      return tb.literalInt(schema.const);
    if (typeof schema.const === "boolean") return tb.literalBool(schema.const);
  }

  if (Array.isArray(schema.enum) && schema.enum.length) {
    return tb.union(
      schema.enum.map((value: unknown) => {
        if (typeof value === "string") return tb.literalString(value);
        if (typeof value === "number" && Number.isInteger(value)) return tb.literalInt(value);
        if (typeof value === "boolean") return tb.literalBool(value);
        if (value === null) return tb.null();
        return tb.string();
      }),
    );
  }

  const alternatives = schema.anyOf ?? schema.oneOf;
  if (Array.isArray(alternatives) && alternatives.length) {
    return tb.union(
      alternatives.map((item: any, index: number) =>
        schemaToBaml(tb, item, `${nameHint}Option${index + 1}`, counter),
      ),
    );
  }

  if (Array.isArray(schema.type)) {
    return tb.union(
      schema.type.map((type: string, index: number) =>
        schemaToBaml(tb, { ...schema, type }, `${nameHint}Type${index + 1}`, counter),
      ),
    );
  }

  if (schema.type === "string") return tb.string();
  if (schema.type === "integer") return tb.int();
  if (schema.type === "number") return tb.float();
  if (schema.type === "boolean") return tb.bool();
  if (schema.type === "null") return tb.null();

  if (schema.type === "array" || schema.items) {
    const itemSchema = Array.isArray(schema.items)
      ? { anyOf: schema.items }
      : schema.items;
    return tb.list(schemaToBaml(tb, itemSchema, `${nameHint}Item`, counter));
  }

  if (schema.type === "object" || schema.properties || schema.additionalProperties) {
    const properties = schema.properties || {};
    if (!Object.keys(properties).length && schema.additionalProperties) {
      const valueType = schema.additionalProperties === true
        ? scalarFallback(tb)
        : schemaToBaml(tb, schema.additionalProperties, `${nameHint}Value`, counter);
      return tb.map(tb.string(), valueType);
    }

    const className = `${safeTypeName(nameHint)}${counter.value++}`;
    const objectClass = tb.addClass(className);
    const required = new Set<string>(Array.isArray(schema.required) ? schema.required : []);
    for (const [propertyName, propertySchema] of Object.entries<any>(properties)) {
      let propertyType = schemaToBaml(
        tb,
        propertySchema,
        `${className}${safeTypeName(propertyName)}`,
        counter,
      );
      if (!required.has(propertyName)) propertyType = propertyType.optional();
      const property = objectClass.addProperty(propertyName, propertyType);
      if (propertySchema?.description) property.description(String(propertySchema.description));
    }
    return objectClass.type();
  }

  return scalarFallback(tb);
}

function buildToolTypes(context: Context): TypeBuilder {
  const tb = new TypeBuilder();
  const counter = { value: 1 };
  const actions: FieldType[] = [];

  for (const [index, tool] of (context.tools || []).entries()) {
    const prefix = `${safeTypeName(tool.name)}${index + 1}`;
    const callClass = tb.addClass(`${prefix}Call`);
    callClass
      .addProperty("tool", tb.literalString(tool.name))
      .description(tool.description || `Call the ${tool.name} tool`);
    callClass.addProperty(
      "arguments",
      schemaToBaml(tb, tool.parameters, `${prefix}Arguments`, counter),
    );
    actions.push(callClass.type());
  }

  actions.push(tb.FinalAnswer.type());
  tb.DynamicDecision
    .addProperty("action", tb.union(actions))
    .description("Select exactly one available tool call or a final answer.");
  return tb;
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

function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw new Error("Request was aborted");
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function emitTextResult(
  stream: AssistantMessageEventStream,
  output: AssistantMessage,
  text: string,
): void {
  if (!text.trim()) throw new Error("BAML returned an empty final answer");
  const block = { type: "text" as const, text: "" };
  output.content.push(block);
  const contentIndex = output.content.indexOf(block);
  stream.push({ type: "text_start", contentIndex, partial: output });
  block.text = text;
  stream.push({ type: "text_delta", contentIndex, delta: text, partial: output });
  stream.push({ type: "text_end", contentIndex, content: block.text, partial: output });
  output.stopReason = "stop";
  stream.push({ type: "done", reason: output.stopReason, message: output });
}

function emitToolResult(
  stream: AssistantMessageEventStream,
  output: AssistantMessage,
  name: string,
  args: Record<string, unknown>,
): void {
  const toolCall = {
    type: "toolCall" as const,
    id: `exa-enhanced-${crypto.randomUUID()}`,
    name,
    arguments: {} as Record<string, unknown>,
  };
  output.content.push(toolCall);
  const contentIndex = output.content.indexOf(toolCall);
  output.stopReason = "toolUse";
  stream.push({ type: "toolcall_start", contentIndex, partial: output });
  toolCall.arguments = args;
  stream.push({
    type: "toolcall_delta",
    contentIndex,
    delta: JSON.stringify(args),
    partial: output,
  });
  stream.push({ type: "toolcall_end", contentIndex, toolCall, partial: output });
  stream.push({ type: "done", reason: output.stopReason, message: output });
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
      throwIfAborted(options?.signal);
      const tb = buildToolTypes(context);
      const request = await b.request.NextAction(contextToText(context), {
        tb,
        env: { OPENAI_API_KEY: "unused" },
      });
      const prompt = renderedPrompt(request.body.json());
      throwIfAborted(options?.signal);
      const rawText = await askExa(prompt, options?.signal);
      throwIfAborted(options?.signal);
      const modelText = stripExaFollowups(rawText);
      let parsed: any;
      try {
        parsed = b.parse.NextAction(modelText, {
          tb,
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
            tb,
            env: { OPENAI_API_KEY: "unused" },
          });
        } catch (repairError) {
          if (!modelText.trim()) {
            throw new AggregateError(
              [firstParseError, repairError],
              "BAML failed to parse both the original and repaired responses",
            );
          }
          parsed = { action: { tool: "final", content: modelText } };
        }
      }

      throwIfAborted(options?.signal);
      parsed = parsed.action;
      if (!isPlainObject(parsed) || typeof parsed.tool !== "string")
        throw new Error("BAML returned an invalid action object");

      if (parsed.tool === "final") {
        if (typeof parsed.content !== "string")
          throw new Error("BAML returned a final action without text content");
        emitTextResult(stream, output, parsed.content);
      } else {
        if (!context.tools?.some((tool: any) => tool.name === parsed.tool))
          throw new Error(`BAML selected unavailable tool: ${parsed.tool}`);
        if (!isPlainObject(parsed.arguments))
          throw new Error(`BAML returned invalid arguments for tool: ${parsed.tool}`);
        const argumentsWithoutNulls = Object.fromEntries(
          Object.entries(parsed.arguments).filter(([, value]) => value != null),
        );
        emitToolResult(stream, output, parsed.tool, argumentsWithoutNulls);
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
