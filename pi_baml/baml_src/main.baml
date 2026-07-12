generator typescript {
  output_type "typescript"
  output_dir "../"
  version "0.223.0"
  module_format "esm"
}

class BashArguments {
  command string
  timeout int?
}

class BashCall {
  tool "bash" @description("Select this whenever a shell command must run. Never claim a command ran in a final answer.")
  arguments BashArguments
}

class ReadArguments {
  path string
  offset int?
  limit int?
}

class ReadCall {
  tool "read" @description("Select this whenever file contents must be inspected before answering.")
  arguments ReadArguments
}

class EditArguments {
  path string
  oldText string
  newText string
}

class EditCall {
  tool "edit" @description("Select this to replace exact existing text in a file.")
  arguments EditArguments
}

class WriteArguments {
  path string
  content string
}

class WriteCall {
  tool "write" @description("Select this to create or completely overwrite a file.")
  arguments WriteArguments
}

class FinalAnswer {
  tool "final" @description("Use only when no tool is needed or all required tool results already appear in the conversation.")
  content string
}

function NextAction(context: string) -> BashCall | ReadCall | EditCall | WriteCall | FinalAnswer {
  client "openai/gpt-4o-mini"
  prompt #"
    You are the decision layer for the Pi coding agent. Continue the supplied
    conversation by selecting exactly one tool action, or return a final answer
    when no tool is needed. If the user asks to run, inspect, edit, or write
    something and no matching result is present yet, you MUST select the tool.
    Never invent a tool result and never repeat an action that already has one.
    Do not output markdown follow-up suggestions.

    <conversation>
    {{ context }}
    </conversation>

    {{ ctx.output_format }}
  "#
}
