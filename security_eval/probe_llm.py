# -*- coding: utf-8 -*-
"""LLM 探活：验证 .env 配置的模型渠道可用性（Phase 2 基线运行前置检查）。

不打印任何密钥。只报告：模型名 / base_url 渠道 / 是否可用 / 错误摘要 / 延迟。

注意：kimi-for-coding 只接受 temperature=1/top_p=0.95，其余取值 400。
统一走项目内的 get_llm_client()（自动丢弃白名单外参数），与线上行为一致。
"""

import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from app.core.llm_client import get_llm_client  # noqa: E402


async def probe(client, model: str) -> dict:
    t0 = time.time()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "你好，请回复\"探活成功\"四个字。"}],
            max_tokens=2048,  # 推理模型：reasoning_content 也消耗 token，给足预算
            stream=False,
        )
        msg = resp.choices[0].message
        text = (msg.content or "").strip()
        reasoning = getattr(msg, "reasoning_content", None) or ""
        if not text and reasoning:
            text = f"(reasoning {len(reasoning)} chars, content empty—token budget?)"
        return {"model": model, "ok": True, "reply": text[:60], "latency_s": round(time.time() - t0, 2)}
    except Exception as e:  # noqa: BLE001
        return {"model": model, "ok": False, "error": str(e)[:160], "latency_s": round(time.time() - t0, 2)}


async def amain() -> int:
    api_key = os.environ.get("ALI_API_KEY")
    base_url = os.environ.get("ALI_BASE_URL")
    if not api_key or not base_url:
        print("FAIL: ALI_API_KEY / ALI_BASE_URL 未配置（.env 未加载？）")
        return 1

    client = get_llm_client(api_key, base_url)
    models = [
        os.environ.get("KEYWORD_EXTRACTION_MODEL", "kimi-for-coding"),
        os.environ.get("TOOL_INTENT_MODEL", "kimi-for-coding"),
        os.environ.get("MODEL_L1", "kimi-for-coding"),
        os.environ.get("MODEL_L2", "kimi-for-coding"),
        os.environ.get("MODEL_L3", "kimi-for-coding"),
    ]
    all_ok = True
    for m in models:
        r = await probe(client, m)
        status = "OK " if r["ok"] else "FAIL"
        if not r["ok"]:
            all_ok = False
        extra = f"reply={r['reply']!r}" if r["ok"] else f"error={r['error']!r}"
        print(f"[{status}] {m:<24} {extra} ({r['latency_s']}s)")

    print("\nPROBE_RESULT=" + ("ALL_OK" if all_ok else "SOME_FAILED"))
    return 0 if all_ok else 2


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
