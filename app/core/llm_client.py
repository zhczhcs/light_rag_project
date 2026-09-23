"""统一 LLM 客户端工厂：抹平不同 OpenAI 兼容服务的参数差异。

kimi-for-coding 参数白名单极严：temperature 仅允许 1、top_p 仅允许 0.95，
其余取值直接 400。当 base_url 指向 kimi 时，包装 chat.completions.create
丢弃这些参数（省略即服务端默认值）。
"""

from openai import AsyncOpenAI

_KIMI_DROP_PARAMS = ("temperature", "top_p")


class _CompletionsWrapper:
    def __init__(self, inner, drop_params: bool):
        self._inner = inner
        self._drop_params = drop_params

    async def create(self, **kwargs):
        if self._drop_params:
            for k in _KIMI_DROP_PARAMS:
                kwargs.pop(k, None)
        return await self._inner.create(**kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _ChatWrapper:
    def __init__(self, inner, drop_temperature: bool):
        self.completions = _CompletionsWrapper(inner.completions, drop_temperature)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class LLMClient:
    def __init__(self, api_key: str, base_url: str):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        if base_url and "kimi" in base_url:
            self.chat = _ChatWrapper(self._client.chat, True)
        else:
            self.chat = self._client.chat

    def __getattr__(self, name):
        return getattr(self._client, name)


def get_llm_client(api_key: str, base_url: str) -> LLMClient:
    return LLMClient(api_key, base_url)
