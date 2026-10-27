import type { AgentMessage } from "@earendil-works/pi-agent-core";
import { type Message, uuidv7 } from "@earendil-works/pi-ai";
import type {
  ExtensionAPI,
  ExtensionCommandContext,
  SessionEntry,
} from "@earendil-works/pi-coding-agent";
import {
  BorderedLoader,
  convertToLlm,
  serializeConversation,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const APPLY_COMMAND = "handoff-apply";

const SYSTEM_PROMPT = `You write handoff prompts. Given a conversation and what should happen next, write the first message for a brand new agent session that has no memory of the conversation. The agent will receive it as an instruction and start working immediately.

Include:
1. Context: what the user is trying to achieve, decisions made, approaches that worked or failed, key findings.
2. Files and commands that matter, with paths.
3. State: what is finished, what is in progress, anything that is broken or unverified.
4. Task: the concrete next step.

Be concise and self-contained. Address the new agent directly. Do not add a preamble such as "Here is the prompt". Output the prompt only.`;

let pending: string | null = null;

function entryToMessage(entry: SessionEntry): AgentMessage | undefined {
  if (entry.type === "message") return entry.message;
  if (entry.type === "compaction") {
    return {
      role: "compactionSummary",
      summary: entry.summary,
      tokensBefore: entry.tokensBefore,
      timestamp: new Date(entry.timestamp).getTime(),
    };
  }
  return undefined;
}

function handoffMessages(branch: SessionEntry[]): AgentMessage[] {
  let compactionIndex = -1;
  for (let i = branch.length - 1; i >= 0; i--) {
    if (branch[i].type === "compaction") {
      compactionIndex = i;
      break;
    }
  }
  const entries = (() => {
    if (compactionIndex < 0) return branch;
    const compaction = branch[compactionIndex];
    const firstKept =
      compaction.type === "compaction"
        ? branch.findIndex((entry) => entry.id === compaction.firstKeptEntryId)
        : -1;
    return [
      compaction,
      ...(firstKept >= 0 ? branch.slice(firstKept, compactionIndex) : []),
      ...branch.slice(compactionIndex + 1),
    ];
  })();
  return entries
    .map(entryToMessage)
    .filter((message): message is AgentMessage => message !== undefined);
}

async function generate(
  ctx: ExtensionCommandContext,
  goal: string,
  signal?: AbortSignal,
): Promise<string | null> {
  const messages = handoffMessages(ctx.sessionManager.getBranch());
  if (messages.length === 0) throw new Error("No conversation to hand off");
  const conversation = serializeConversation(convertToLlm(messages));
  const next = goal || "Continue the work from where the conversation stopped.";
  const userMessage: Message = {
    role: "user",
    content: [
      {
        type: "text",
        text: `## Conversation History\n\n${conversation}\n\n## What Happens Next\n\n${next}`,
      },
    ],
    timestamp: Date.now(),
  };
  const response = await ctx.modelRegistry.complete(
    ctx.model!,
    { systemPrompt: SYSTEM_PROMPT, messages: [userMessage] },
    { signal, cacheRetention: "none", sessionId: uuidv7() },
  );
  if (response.stopReason === "aborted") return null;
  if (response.stopReason === "error")
    throw new Error(response.errorMessage || "Model call failed");
  const text = response.content
    .filter((part): part is { type: "text"; text: string } => part.type === "text")
    .map((part) => part.text)
    .join("\n")
    .trim();
  if (!text) throw new Error("Model returned an empty handoff prompt");
  return text;
}

async function generateWithUi(
  ctx: ExtensionCommandContext,
  goal: string,
): Promise<string | null> {
  if (ctx.mode !== "tui") return generate(ctx, goal);
  let failure: unknown;
  const result = await ctx.ui.custom<string | null>((tui, theme, _kb, done) => {
    const loader = new BorderedLoader(tui, theme, "Generating handoff prompt...");
    loader.onAbort = () => done(null);
    generate(ctx, goal, loader.signal)
      .then(done)
      .catch((error) => {
        failure = error;
        done(null);
      });
    return loader;
  });
  if (failure) throw failure;
  return result;
}

async function startFresh(ctx: ExtensionCommandContext, prompt: string): Promise<void> {
  const parentSession = ctx.sessionManager.getSessionFile();
  const result = await ctx.newSession({
    parentSession,
    withSession: async (next) => {
      next.sendUserMessage(prompt).catch((error: unknown) => {
        next.ui.notify(
          `Handoff prompt was not sent: ${error instanceof Error ? error.message : String(error)}`,
          "error",
        );
      });
    },
  });
  if (result.cancelled) ctx.ui.notify("New session cancelled", "info");
}

export default function (pi: ExtensionAPI) {
  pi.registerCommand("handoff", {
    description:
      "Summarize this session into a handoff prompt, start a new session and run it: /handoff [what happens next]",
    handler: async (args, ctx) => {
      if (!ctx.model) {
        ctx.ui.notify("No model selected", "error");
        return;
      }
      try {
        await ctx.waitForIdle();
        const prompt = await generateWithUi(ctx, args.trim());
        if (prompt === null) {
          ctx.ui.notify("Cancelled", "info");
          return;
        }
        await startFresh(ctx, prompt);
      } catch (error) {
        ctx.ui.notify(
          `Handoff failed: ${error instanceof Error ? error.message : String(error)}`,
          "error",
        );
      }
    },
  });

  pi.registerCommand(APPLY_COMMAND, {
    description: "Start the session queued by the handoff tool",
    handler: async (_args, ctx) => {
      const prompt = pending;
      pending = null;
      if (!prompt) {
        ctx.ui.notify("No handoff is queued", "info");
        return;
      }
      try {
        await startFresh(ctx, prompt);
      } catch (error) {
        ctx.ui.notify(
          `Handoff failed: ${error instanceof Error ? error.message : String(error)}`,
          "error",
        );
      }
    },
  });

  pi.registerTool({
    name: "handoff",
    label: "Handoff",
    description:
      "Clear the conversation and continue in a fresh session. You write the handoff prompt, it is sent to the new session as its first message and runs immediately. The old conversation is not visible afterwards.",
    promptSnippet: "Continue in a fresh session with a handoff prompt you write",
    promptGuidelines: [
      "Use handoff only when the context is very long, cluttered with dead ends, or the user asked for a fresh start. Do not use it to avoid finishing work.",
      "A handoff prompt must be self-contained: goal, decisions, relevant files and commands, current state, and the exact next step.",
    ],
    parameters: Type.Object({
      prompt: Type.String({
        description:
          "Complete first message for the new session. It cannot see this conversation.",
      }),
    }),
    async execute(_toolCallId, params) {
      const prompt = params.prompt.trim();
      if (!prompt) throw new Error("The handoff prompt is empty");
      pending = prompt;
      pi.sendUserMessage(`/${APPLY_COMMAND}`, {
        deliverAs: "followUp",
        expandPromptTemplates: true,
      });
      return {
        content: [
          {
            type: "text",
            text: "Handoff queued. A new session starts when this turn ends and will receive your prompt.",
          },
        ],
        details: undefined,
        terminate: true,
      };
    },
  });
}
