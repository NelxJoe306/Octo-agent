from collections.abc import Awaitable, Callable
from typing import Literal

from agent_core.tools import AgentTool, AgentToolResult

Mode = Literal["execute", "plan"]
MODE_NAMESPACE = "octo.mode"
MODE_TOOL_NAMES = ("enter_plan_mode", "exit_plan_mode")


def mode_tools(set_mode: Callable[[Mode], Awaitable[None]]) -> list[AgentTool]:
    def tool(name: str, mode: Mode) -> AgentTool:
        async def execute(call_id, arguments, signal=None, on_update=None):
            if signal is not None and signal.is_cancelled():
                return AgentToolResult(content="Mode change cancelled", is_error=True)
            await set_mode(mode)
            return AgentToolResult(content=f"Mode is now {mode}")
        return AgentTool(name=name, label=name,
            description=("Enter read-only planning. Write tools and general shell are blocked."
                         if mode == "plan" else "Finish planning and resume tool execution."),
            parameters={"type":"object", "properties":{}, "additionalProperties":False},
            execute_fn=execute, access="control")
    return [tool("enter_plan_mode", "plan"), tool("exit_plan_mode", "execute")]
