from __future__ import annotations

import json
import logging
from string import Template
from typing import Any, List, Optional, Dict, Union

from colorama import Fore

from minichain.agent.base_agent import BaseAgent
from minichain.agent.message import UserMessage, BaseMessage
from minichain.agent.support_agent.output_parser import SupportJSONOutputParser
from minichain.agent.support_agent.prompt import (
    FIX_TOOL_INPUT_PROMPT_FORMAT,
    SHOULD_ANSWER_PROMPT,
    CLARIFYING_QUESTION_PROMPT,
    STEP_BY_STEP_PROMPT,
)
from minichain.agent.prompt_formatter import JSONPromptTemplate
from minichain.agent.structs import AgentAction, AgentFinish
from minichain.models.base import Generation, BaseLanguageModel
from minichain.tools.base import Tool
from minichain.tools.simple_handoff.tools import HandOffToAgent
from minichain.utils import print_with_color

logger = logging.getLogger(__name__)


class SupportAgent(BaseAgent):
    output_parser: SupportJSONOutputParser = SupportJSONOutputParser()
    llm: BaseLanguageModel = None
    prompt_template: JSONPromptTemplate = None
    allowed_tools: Dict[str, Tool] = {}
    tools: List[Tool] = []

    # injected policy into the prompt
    policy: str = ""

    @classmethod
    def from_llm_and_tools(
        cls,
        llm: BaseLanguageModel,
        tools: List[Tool] = None,
        output_parser: Optional[SupportJSONOutputParser] = None,
        prompt: str = STEP_BY_STEP_PROMPT,
        input_variables: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> SupportAgent:
        """Construct an agent from an LLM and tools."""
        tools = tools or []
        tools.append(HandOffToAgent())

        prompt_template = cls.get_prompt_template(
            prompt=prompt,
            input_variables=input_variables,
        )

        allowed_tools = {tool.name: tool for tool in tools}
        _output_parser = output_parser or SupportJSONOutputParser()
        return cls(
            llm=llm,
            allowed_tools=allowed_tools,
            output_parser=_output_parser,
            prompt_template=prompt_template,
            tools=tools,
            **kwargs,
        )

    def should_answer(
        self, should_answer_prompt_template: str = SHOULD_ANSWER_PROMPT, **kwargs
    ) -> Optional[AgentFinish]:
        """Determine if agent should continue to answer user questions based on the latest user
        query"""
        if "query" not in kwargs or "history" not in kwargs or not kwargs["history"]:
            return None

        def _parse_response(res: str):
            if "yes" in res.lower():
                return AgentFinish(
                    message="Thank your for contacting",
                    log=f"Thank your for contacting",
                )
            else:
                return None

        prompt = Template(should_answer_prompt_template).substitute(**kwargs)
        response = (
            self.llm.generate([UserMessage(content=prompt)])
            .generations[0]
            .message.content
        )
        return _parse_response(response)

    def get_final_prompt(
        self,
        template: JSONPromptTemplate,
        intermediate_steps: List[AgentAction],
        **kwargs: Any,
    ) -> List[BaseMessage]:
        def _construct_scratchpad(
            actions: List[AgentAction],
        ) -> Union[str, List[BaseMessage]]:
            scratchpad = ""
            for action in actions:
                scratchpad += action.response
            return scratchpad

        """Create the full inputs for the LLMChain from intermediate steps."""
        thoughts = _construct_scratchpad(intermediate_steps)
        new_inputs = {"agent_scratchpad": thoughts}
        full_inputs = {**kwargs, **new_inputs}
        prompt = template.format_prompt(**full_inputs)
        return prompt

    @staticmethod
    def get_prompt_template(
        prompt: str = "",
        input_variables: Optional[List[str]] = None,
    ) -> JSONPromptTemplate:
        """Create prompt in the style of the zero shot agent.

        Args:
            prompt: message to be injected between prefix and suffix.
            input_variables: List of input variables the final prompt will expect.

        Returns:
            A PromptTemplate with the template assembled from the pieces here.
        """
        template = Template(prompt)

        if input_variables is None:
            input_variables = ["input", "chat_history", "agent_scratchpad"]
        return JSONPromptTemplate(template=template, input_variables=input_variables)

    def plan(
        self, intermediate_steps: List[AgentAction], **kwargs: Any
    ) -> Union[AgentAction, AgentFinish]:
        """
        Plan the next step. either taking an action with AgentAction or respond to user with AgentFinish
        Args:
            intermediate_steps: List of AgentAction that has been performed with outputs
            **kwargs: key value pairs from chain, which contains query and other stored memories

        Returns:
            AgentAction or AgentFinish
        """
        print_with_color(f"Planning", Fore.LIGHTYELLOW_EX)
        tool_names = ", ".join([tool.name for tool in self.tools])
        tool_strings = "\n\n".join(
            [f"> {tool.name}: \n{tool.description}" for tool in self.tools]
        )
        inputs = {
            "tool_names": tool_names,
            "tools": tool_strings,
            "policy": self.policy,
            **kwargs,
        }
        final_prompt = self.get_final_prompt(
            self.prompt_template, intermediate_steps, **inputs
        )
        logger.info(f"\nFull Input: {final_prompt[0].content} \n")

        full_output: Generation = self.llm.generate(final_prompt).generations[0]
        agent_output: Union[AgentAction, AgentFinish] = self.output_parser.parse(
            full_output.message.content
        )

        print_with_color(
            f"Full output: {json.loads(full_output.message.content)}", Fore.YELLOW
        )
        if isinstance(agent_output, AgentAction):
            print_with_color(
                f"Plan to take action '{agent_output.tool}'", Fore.LIGHTYELLOW_EX
            )

            # call hand off to agent and finish workflow
            if agent_output.tool == HandOffToAgent().name:
                return AgentFinish(
                    message=HandOffToAgent().run(""), log=f"Handing off to agent"
                )

        return agent_output

    def clarify_args_for_agent_action(
        self,
        agent_action: AgentAction,
        intermediate_steps: List[AgentAction],
        **kwargs: Any,
    ):
        print_with_color(f"Deciding if need clarification", Fore.LIGHTYELLOW_EX)
        inputs = {
            "tool_name": agent_action.tool,
            "tool_desp": self.allowed_tools.get(agent_action.tool).description,
            **kwargs,
        }

        clarifying_template = self.get_prompt_template(
            prompt=CLARIFYING_QUESTION_PROMPT
        )

        final_prompt = self.get_final_prompt(
            clarifying_template, intermediate_steps, **inputs
        )
        logger.info(f"\nClarification inputs: {final_prompt[0].content}")
        full_output: Generation = self.llm.generate(final_prompt).generations[0]
        return self.output_parser.parse_clarification(
            full_output.message.content, agent_action=agent_action
        )

    def fix_action_input(
        self, tool: Tool, action: AgentAction, error: str
    ) -> AgentAction:
        prompt = FIX_TOOL_INPUT_PROMPT_FORMAT.format(
            tool_description=tool.description, inputs=action.tool_input, error=error
        )

        logger.info(f"\nFixing tool input prompt: {prompt}")
        messages = UserMessage(content=prompt)
        output = self.llm.generate([messages])
        text = output.generations[0].message.content
        inputs = text[text.index("{") : text.rindex("}") + 1].strip()
        new_tool_inputs = json.loads(inputs)

        logger.info(f"\nFixed tool output: {new_tool_inputs}")
        new_action = AgentAction(tool=action.tool, tool_input=new_tool_inputs)
        return new_action