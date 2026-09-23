# -*- coding: utf-8 -*-
"""LLM 探活：验证 .env 配置的阿里云百炼模型可用性（Phase 2 基线运行前置检查）。

不打印任何密钥。只报告：模型名 / base_url 主机 / 是否可用 / 错误摘要 / 延迟。
"""

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from openai import OpenAI  # noqa: E402


def probe(client: OpenAI, model: str) -> dict:
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "你好，请回复\"探活成功\"四个字。"}],
            temperature=0,
            max_tokens=30,
            extra_body={"enable_thinking": False},
        )
        text = (resp.choices[0].message.content or "").strip()
        return {"model": model, "ok": True, "reply": text[:40], "latency_s": round(time.time() - t0, 2)}
    except Exception as e:  # noqa: BLE001
        return {"model": model, "ok": False, "error": str(e)[:160], "latency_s": round(time.time() - t0, 2)}


def main() -> int:
    api_key = os.environ.get("ALI_API_KEY")
    base_url = os.environ.get("ALI_BASE_URL")
    if not api_key or not base_url:
        print("FAIL: ALI_API_KEY / ALI_BASE_URL 未配置（.env 未加载？）")
        return 1

    client = OpenAI(api_key=api_key, base_url=base_url)
    models = [
        os.environ.get("KEYWORD_EXTRACTION_MODEL", "qwen3.5-35b-a3b"),
        os.environ.get("TOOL_INTENT_MODEL", "qwen-turbo-latest"),
        os.environ.get("MODEL_L1", "qwen3.6-flash"),
        os.environ.get("MODEL_L2", "qwen3.6-plus-2026-04-02"),
        os.environ.get("MODEL_L3", "kimi-k2.5"),
    ]
    all_ok = True
    for m in models:
        r = probe(client, m)
        status = "OK " if r["ok"] else "FAIL"
        if not r["ok"]:
            all_ok = False
        extra = f"reply={r['reply']!r}" if r["ok"] else f"error={r['error']!r}"
        print(f"[{status}] {r['model']:<28} {extra} ({r['latency_s']}s)")

    print("\nPROBE_RESULT=" + ("ALL_OK" if all_ok else "SOME_FAILED"))
    return 0 if all_ok else 2


if __name__ == "__main__":
    sys.exit(main())
