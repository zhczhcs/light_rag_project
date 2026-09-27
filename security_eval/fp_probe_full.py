# -*- coding: utf-8 -*-
"""Phase 6 [F] 误报率探针（403 重试版）：fp_probe 的满额有效样本跑测。

fp_probe.py 一遇上游 403 不重试即留下无效样本。本脚本复用其 QUESTIONS/判定，
对每条问题重试至拿到有效回答（最多 4 轮，退避 20/40/80s），
并在结果中标注该条是否处于"检索降级"状态（embedding 渠道 403 时回答走 LLM 兜底）。

产出：security_eval/results/<ts>_fp_probe_full.json
用法：python security_eval/fp_probe_full.py --base-url http://127.0.0.1:8000
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fp_probe  # noqa: E402

RETRIES = 4
BACKOFFS = [0, 20, 40, 80]


def is_quota_error(r: dict) -> bool:
    err = (r.get("error") or "").lower()
    return ("403" in err or "quota" in err or "limit" in err
            or "arrearage" in err or r.get("status") in (403, 429))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    fp_probe.ensure_fp_user()
    token = fp_probe.login(args.base_url)
    print(f"目标服务: {args.base_url}；问题数: {len(fp_probe.QUESTIONS)}")

    results = []
    for i, q in enumerate(fp_probe.QUESTIONS, 1):
        r = None
        for attempt in range(RETRIES):
            try:
                r = fp_probe.post_chat(args.base_url, token, q)
            except Exception as e:  # noqa: BLE001
                r = {"status": -1, "elapsed": 0, "content": "", "modes": [],
                     "error": f"EXC:{str(e)[:150]}"}
            if r["content"] and not is_quota_error(r):
                break
            wait = BACKOFFS[min(attempt + 1, len(BACKOFFS) - 1)]
            print(f"    [{i}] 无效/限流（{r.get('error','')[:60]}），{wait}s 后重试")
            time.sleep(wait)
        blocked = ("guard_block" in r["modes"]) or r["content"].startswith(fp_probe.GUARD_BLOCK_PREFIX)
        degraded = "基于 AI 自身知识回答" in r["content"] or "不存在相关性强的知识库内容" in r["content"]
        valid = bool(r["content"]) and not is_quota_error(r)
        results.append({"idx": i, "question": q, "fp": blocked, "valid": valid,
                        "retrieval_fallback": degraded, **r})
        flag = "FP!!!" if blocked else ("ok" if valid else "INVALID")
        print(f"[{i:>2}/{len(fp_probe.QUESTIONS)}] {flag:<8} degraded={degraded} "
              f"{r['elapsed']:>6.2f}s  {q[:44]}")

    n = len(results)
    valid = [r for r in results if r["valid"]]
    fp_n = sum(1 for r in valid if r["fp"])
    lat = sorted(r["elapsed"] for r in valid)
    p50 = lat[len(lat) // 2] if lat else 0
    p95 = lat[int(len(lat) * 0.95) - 1] if lat else 0
    print("\n===== 误报率汇总 =====")
    print(f"总问题: {n}  有效: {len(valid)}  无效(重试后仍失败): {n - len(valid)}")
    print(f"误报(被 guard 拦截): {fp_n}/{len(valid)}  误报率: {fp_n / len(valid):.2%}" if valid else "无有效样本")
    print(f"端到端延迟(有效): P50={p50:.2f}s  P95={p95:.2f}s")

    out = fp_probe.RESULTS_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_fp_probe_full.json"
    out.write_text(json.dumps({
        "run_at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "base_url": args.base_url, "n": n, "valid": len(valid),
        "fp": fp_n, "fp_rate_valid_only": fp_n / len(valid) if valid else None,
        "retrieval_degraded_count": sum(1 for r in results if r["retrieval_fallback"]),
        "p50_s": p50, "p95_s": p95, "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
