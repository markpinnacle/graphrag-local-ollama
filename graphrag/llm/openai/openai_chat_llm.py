# Copyright (c) 2024 Microsoft Corporation.
# Licensed under the MIT License

"""The Chat-based language model."""

import logging
from json import JSONDecodeError

from typing_extensions import Unpack

from graphrag.llm.base import BaseLLM
from graphrag.llm.types import (
    CompletionInput,
    CompletionOutput,
    LLMInput,
    LLMOutput,
)

from ._json import clean_up_json
from ._prompts import JSON_CHECK_PROMPT
from .openai_configuration import OpenAIConfiguration
from .types import OpenAIClientTypes
from .utils import (
    get_completion_llm_args,
    try_parse_json_object,
)

log = logging.getLogger(__name__)

_MAX_GENERATION_RETRIES = 1
FAILED_TO_CREATE_JSON_ERROR = "Failed to generate valid JSON output"


class OpenAIChatLLM(BaseLLM[CompletionInput, CompletionOutput]):
    """A Chat-based LLM."""

    _client: OpenAIClientTypes
    _configuration: OpenAIConfiguration

    def __init__(self, client: OpenAIClientTypes, configuration: OpenAIConfiguration):
        self.client = client
        self.configuration = configuration

    async def _execute_llm(
        self, input: CompletionInput, **kwargs: Unpack[LLMInput]
    ) -> CompletionOutput | None:
        args = get_completion_llm_args(
            kwargs.get("model_parameters"), self.configuration
        )
        history = kwargs.get("history") or []
        messages = [
            *history,
            {"role": "user", "content": input},
        ]
        completion = await self.client.chat.completions.create(
            messages=messages, **args
        )
        print("completion", completion)
        return completion.choices[0].message.content

    async def _invoke_json(
        self,
        input: CompletionInput,
        **kwargs: Unpack[LLMInput],
    ) -> LLMOutput[CompletionOutput]:
        """Generate JSON output."""
        name = kwargs.get("name") or "unknown"
        is_response_valid = kwargs.get("is_response_valid") or (lambda _x: True)

        async def generate(
            attempt: int | None = None,
        ) -> LLMOutput[CompletionOutput]:
            call_name = name if attempt is None else f"{name}@{attempt}"
            return (
                await self._native_json(input, **{**kwargs, "name": call_name})
                if self.configuration.model_supports_json
                else await self._manual_json(input, **{**kwargs, "name": call_name})
            )

        def is_valid(x: dict | None) -> bool:
            return x is not None and is_response_valid(x)

        result = await generate()
        retry = 0
        while not is_valid(result.json) and retry < _MAX_GENERATION_RETRIES:
            result = await generate(retry)
            retry += 1
        print("result", result.json)
        if is_valid(result.json):
            return result
        raise RuntimeError(FAILED_TO_CREATE_JSON_ERROR)

    async def _native_json(
        self, input: CompletionInput, **kwargs: Unpack[LLMInput]
    ) -> LLMOutput[CompletionOutput]:
        """Generate JSON output using a model's native JSON-output support."""
        print(self._invoke)
        result = await self._invoke(
            input,
            **{
                **kwargs,
                "model_parameters": {
                    **(kwargs.get("model_parameters") or {}),
                    "response_format": {"type": "json_object"},
                },
            },
        )
        print("result", result)
        raw_output = result.output or ""
        json_output = try_parse_json_object(raw_output)

        return LLMOutput[CompletionOutput](
            output=raw_output,
            json=json_output,
            history=result.history,
        )

    async def _manual_json(
        self, input: CompletionInput, **kwargs: Unpack[LLMInput]
    ) -> LLMOutput[CompletionOutput]:
        # Otherwise, clean up the output and try to parse it as json
        result = await self._invoke(input, **kwargs)
        history = result.history or []
        output = clean_up_json(result.output or "")
        try:
            json_output = try_parse_json_object(output)
            return LLMOutput[CompletionOutput](
                output=output, json=json_output, history=history
            )
        except (TypeError, JSONDecodeError):
            log.warning("error parsing llm json, retrying")
            # If cleaned up json is unparsable, use the LLM to reformat it (may throw)
            result = await self._try_clean_json_with_llm(output, **kwargs)
            output = clean_up_json(result.output or "")
            json = try_parse_json_object(output)

            return LLMOutput[CompletionOutput](
                output=output,
                json=json,
                history=history,
            )

    async def _try_clean_json_with_llm(
        self, output: str, **kwargs: Unpack[LLMInput]
    ) -> LLMOutput[CompletionOutput]:
        name = kwargs.get("name") or "unknown"
        return await self._invoke(
            JSON_CHECK_PROMPT,
            **{
                **kwargs,
                "variables": {"input_text": output},
                "name": f"fix_json@{name}",
            },
        )
