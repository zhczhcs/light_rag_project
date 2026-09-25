# -*- coding: utf-8 -*-
"""Phase 4 [D] 结果分析：防护前后 ASR 对比 + 扫描延迟统计。

读取：
  - 基线结果 security_eval/results/20260923T092237Z_direct_injection.json（基线）
                          /20260923T102843Z_indirect_injection.json（基线）
  - 本轮结果 security_eval/results/<最新>_direct_injection.json / _indirect_injection.json
  - security_eval/scan_log.jsonl（扫描延迟与决策）

输出一段可直接粘进 phase4_report.md 的 Markdown。
用法：
  python security_eval/analyze_phase4.py \
      --base-direct  results/20260923T092237Z_direct_injection.json \
      --base-indirect results/20260923T102843Z_indirect_injection.json \
      --new-direct  results/<new>_direct_injection.json \
      --new-indirect results/<new>_indirect_injection.json
"""

import argparse
import json
import statistics as st
import sys
from pathlib import Path

RES = Path(__file__).resolve().parent / "results"
LOG = Path(__file__).resolve().parent / "scan_log.jsonl"


def load(p: str) -> dict:
    return json.loads((RES / Path(p).name).read_text(encoding="utf-8"))


def asr_block(run: dict, cat: str) -> dict:
    rows = [r for r in run["results"] if r["category"] == cat]
    n = len(rows)
    succ = sum(1 for r in rows if r["success"])
    err = sum(1 for r in rows if r["status"] == "error")
    lat = sorted(r["elapsed_s"] for r in rows if "elapsed_s" in r)
    per_pass = {}
    for p in sorted({r.get("pass", 1) for r in rows}):
        pr = [r for r in rows if r.get("pass", 1) == p]
        per_pass[p] = f"{sum(1 for r in pr if r['success'])}/{len(pr)}"
    return {
        "n": n, "succ": succ, "err": err,
        "asr": f"{succ}/{n} = {succ / n:.2%}" if n else "-",
        "asr_completed": f"{succ}/{n - err} = {succ / (n - err):.2%}" if n - err else "-",
        "per_pass": per_pass,
        "lat_median": f"{st.median(lat):.2f}s" if lat else "-",
        "lat_p95": f"{lat[min(len(lat) - 1, int(len(lat) * 0.95) - 1)]:.2f}s" if lat else "-",
        "poison_hit": (f"{sum(1 for r in rows if r.get('poison_hit'))}/"
                       f"{sum(1 for r in rows if 'poison_hit' in r)}"
                       if any("poison_hit" in r for r in rows) else None),
    }


def scan_latency() -> dict:
    if not LOG.exists():
        return {}
    recs = [json.loads(l) for l in LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    out = {"total_records": len(recs)}
    for stype in ("input", "retrieval", "output", "citation"):
        rs = [r for r in recs if r.get("scan_type") == stype]
        if not rs:
            continue
        lats = sorted(r.get("latency_ms", 0.0) for r in rs)
        entry = {
            "n": len(rs),
            "decisions": {},
            "latency_median_ms": round(st.median(lats), 1),
            "latency_p95_ms": round(lats[min(len(lats) - 1, int(len(lats) * 0.95) - 1)], 1) if lats else None,
            "latency_max_ms": round(max(lats), 1) if lats else None,
        }
        for r in rs:
            d = r.get("decision", "?")
            entry["decisions"][d] = entry["decisions"].get(d, 0) + 1
        if stype == "input":
            entry["blocked_by"] = {
                "rule_only": sum(1 for r in rs if r.get("decision") == "block" and r.get("rule_hits") and (r.get("model_conf") or 0) < 0.5),
                "model_only": sum(1 for r in rs if r.get("decision") == "block" and not r.get("rule_hits") and (r.get("model_conf") or 0) >= 0.5),
                "both": sum(1 for r in rs if r.get("decision") == "block" and r.get("rule_hits") and (r.get("model_conf") or 0) >= 0.5),
            }
        if stype == "retrieval":
            entry["flagged_items"] = sum(len(r.get("flagged_items") or []) for r in rs)
            # 异步扫描含模型冷启动的离群值另列（服务预热后首个请求之外应很小）
            warm = sorted(r.get("latency_ms", 0.0) for r in rs if r.get("latency_ms", 0) < 5000)
            if warm:
                entry["latency_median_warm_ms"] = round(st.median(warm), 1)
        out[stype] = entry
    return out


def md_table(rows: list[list[str]]) -> str:
    return "\n".join("| " + " | ".join(r) + " |" for r in rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-direct", required=True)
    ap.add_argument("--base-indirect", required=True)
    ap.add_argument("--new-direct", required=True)
    ap.add_argument("--new-indirect", required=True)
    args = ap.parse_args()

    bd, bi, nd, ni = (load(p) for p in (args.base_direct, args.base_indirect,
                                        args.new_direct, args.new_indirect))

    print("## ASR 对比（基线 vs Phase 4 防护后）\n")
    header = ["攻击类别", "样本×轮", "基线 ASR", "防护后 ASR", "基线中位耗时", "防护后中位耗时", "分轮(防护后)"]
    rows = [header, ["---"] * len(header)]
    for cat, b, n in (("direct_injection", bd, nd), ("indirect_injection", bi, ni)):
        bb, nn = asr_block(b, cat), asr_block(n, cat)
        rows.append([
            cat,
            f"{bb['n']}",
            f"**{bb['asr']}**",
            f"**{nn['asr']}**（error={nn['err']}）",
            bb["lat_median"], nn["lat_median"],
            ", ".join(f"p{k}:{v}" for k, v in nn["per_pass"].items()),
        ])
    print(md_table(rows))
    for cat, b, n in (("direct_injection", bd, nd), ("indirect_injection", bi, ni)):
        bb, nn = asr_block(b, cat), asr_block(n, cat)
        if bb["poison_hit"] or nn["poison_hit"]:
            print(f"\n- {cat} 投毒命中率: 基线 {bb['poison_hit']} → 防护后 {nn['poison_hit']}")

    print("\n## 扫描决策日志统计（scan_log.jsonl）\n")
    info = scan_latency()
    rows = [["扫描类型", "记录数", "决策分布", "中位延迟", "P95 延迟", "最大延迟"]]
    rows.append(["---"] * 6)
    for stype in ("input", "retrieval", "output", "citation"):
        d = info.get(stype)
        if not d:
            continue
        decisions = ", ".join(f"{k}:{v}" for k, v in d["decisions"].items())
        extra = ""
        if stype == "retrieval" and d.get("latency_median_warm_ms") is not None:
            extra = f"（暖机后中位 {d['latency_median_warm_ms']}ms，异步不在请求路径）"
        rows.append([stype, str(d["n"]), decisions,
                     f"{d['latency_median_ms']}ms{extra}",
                     f"{d['latency_p95_ms']}ms", f"{d['latency_max_ms']}ms"])
    print(md_table(rows))
    if "input" in info and info["input"].get("blocked_by"):
        print(f"\n- 输入拦截归因: {info['input']['blocked_by']}（rule_only=规则层独立命中, model_only=模型独立命中, both=双命中）")
    if "retrieval" in info:
        print(f"- 检索扫描标记出的可疑条目总数: {info['retrieval'].get('flagged_items')}")
    print(f"- 扫描日志总记录数: {info.get('total_records')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
