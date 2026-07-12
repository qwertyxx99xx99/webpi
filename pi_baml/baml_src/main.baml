generator typescript {
  output_type "typescript"
  output_dir "../"
  version "0.223.0"
  module_format "esm"
}

class FinalAnswer {
  tool "final" @description("Use only when no tool is needed or all required tool results already appear in the conversation.")
  content string
}

class DynamicDecision {
  @@dynamic
}

function NextAction(context: string) -> DynamicDecision {
  client "openai/gpt-4o-mini"
  prompt #"
    You are the decision layer for the Pi coding agent. Continue the supplied
    conversation by selecting exactly one action from the dynamically supplied
    tool union, or return a final answer when no tool is needed. If the user
    asks to run, inspect, edit, or write something and no matching result is
    present yet, you MUST select a tool. Never invent a tool result and never
    repeat an action that already has one. Do not output follow-up suggestions.

    <conversation>
    {{ context }}
    </conversation>

    {{ ctx.output_format }}
  "#
}
