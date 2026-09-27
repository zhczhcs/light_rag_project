# -*- coding: utf-8 -*-
"""Phase 6 [F] 中文对比实验：三个检测器离线对比（不经过系统，直接喂样本）。

检测器：
  A. 线上中文 BERT  bixuechao/bert_cn_prompt_attack_detection（P(attack)>=0.5 命中）
  B. PromptGuard 2 替代品 protectai/deberta-v3-base-prompt-injection-v2
     （PromptGuard 2 经 hf-mirror 拉取仍 401 gated，按计划 §7 回退；见 final_report）
  C. 纯规则层（app/core/guard_service.py INJECTION_RULES，11 条，任一条命中即判）

样本：
  中文攻击 = JailBench（STAIR-BUPT, PAKDD 2025，arXiv:2502.18935）公开子集 2376 条中
             按一级领域分层抽样 200 条（seed=42） + 本仓库 attacks.jsonl direct_injection 中文 16 条
  英文攻击 = attacks.jsonl direct_injection 英文 16 条
  中文良性 = fp_probe.py 正常问题 30 条（含刁钻项）
  英文良性 = fp_probe.py 正常问题 20 条

产出：security_eval/results/cn_detector_compare.json（逐样本分数 + 汇总表）
用法：python security_eval/cn_detector_compare.py [--jailbench-csv path] [--full]（--full 跑全量 2376 条 JailBench）
"""

import argparse
import csv
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.guard_service import _CNInjectionClassifier, _match_injection_rules  # noqa: E402
from security_eval.fp_probe import QUESTIONS as FP_QUESTIONS  # noqa: E402

ATTACKS_PATH = Path(__file__).resolve().parent / "attacks.jsonl"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

JAILBENCH_N = 200
JAILBENCH_SEED = 42
CN_BERT_THRESHOLD = 0.5

# 中文判定：CJK 字符占比启发式（fp_probe 前 30 条中文、后 20 条英文，与注释一致）


def load_attack_samples() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"zh": [], "en": []}
    for line in open(ATTACKS_PATH, encoding="utf-8"):
        s = json.loads(line)
        if s["category"] != "direct_injection":
            continue
        out["zh" if s["language"] == "zh" else "en"].append(
            {"id": s["id"], "text": s["payload"], "source": "attacks.jsonl"})
    return out


def load_jailbench(path: Path, n: int, seed: int) -> list[dict]:
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cat[r["一级领域"]].append(r)
    rng = random.Random(seed)
    per = max(1, n // len(by_cat))
    picked: list[dict] = []
    idx = 0
    for cat, items in sorted(by_cat.items()):
        rng.shuffle(items)
        for r in items[:per]:
            picked.append({"id": f"jailbench-{idx:04d}", "text": r["query"],
                           "source": "JailBench-public",
                           "category_l1": cat, "category_l2": r["二级领域"]})
            idx += 1
    return picked


class DebertaDetector:
    """protectai/deberta-v3-base-prompt-injection-v2（本地 snapshot）。"""

    def __init__(self, local_dir: str):
        self.tok = AutoTokenizer.from_pretrained(local_dir)
        self.model = AutoModelForSequenceClassification.from_pretrained(local_dir)
        self.model.eval()
        print(f"deberta 标签映射: {self.model.config.id2label}")

    @torch.no_grad()
    def predict(self, texts: list[str]) -> list[float]:
        """返回 P(INJECTION)。"""
        enc = self.tok(texts, return_tensors="pt", truncation=True, max_length=512, padding=True)
        logits = self.model(**enc).logits
        probs = torch.softmax(logits, dim=-1)
        inj_id = next(i for i, lab in self.model.config.id2label.items()
                      if "INJECTION" in str(lab).upper())
        return probs[:, inj_id].tolist()


def batched(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jailbench-csv", default=str(Path("JailBench.csv")))
    ap.add_argument("--deberta-dir", required=True, help="本地 snapshot 路径")
    ap.add_argument("--full", action="store_true", help="JailBench 全量（默认分层 200 条）")
    args = ap.parse_args()

    direct = load_attack_samples()
    jailbench = load_jailbench(Path(args.jailbench_csv), 10_000 if args.full else JAILBENCH_N, JAILBENCH_SEED)
    zh_benign = [{"id": f"fp-zh-{i+1}", "text": q, "source": "fp_probe"} for i, q in enumerate(FP_QUESTIONS[:30])]
    en_benign = [{"id": f"fp-en-{i+1}", "text": q, "source": "fp_probe"} for i, q in enumerate(FP_QUESTIONS[30:])]

    sets = {
        "zh_attack_jailbench": jailbench,
        "zh_attack_local": direct["zh"],
        "en_attack_local": direct["en"],
        "zh_benign": zh_benign,
        "en_benign": en_benign,
    }
    for k, v in sets.items():
        print(f"{k}: {len(v)} 条")

    cn = _CNInjectionClassifier.get()
    deb = DebertaDetector(args.deberta_dir)

    all_rows: list[dict] = []
    t0 = time.time()
    for set_name, items in sets.items():
        texts = [it["text"] for it in items]
        cn_scores: list[float] = []
        deb_scores: list[float] = []
        for batch in batched(texts, 16):
            # predict 返回 [(P(注入), 该批次延迟ms), ...]
            cn_scores.extend(p for p, _ in cn.predict(batch))
            deb_scores.extend(deb.predict(batch))
        for it, cns, dbs in zip(items, cn_scores, deb_scores):
            all_rows.append({
                **it, "set": set_name,
                "cn_bert_p_attack": round(cns, 6),
                "cn_bert_hit": cns >= CN_BERT_THRESHOLD,
                "deberta_p_injection": round(dbs, 6),
                "deberta_hit": dbs >= 0.5,
                "rule_hits": _match_injection_rules(it["text"]),
                "rule_hit": bool(_match_injection_rules(it["text"])),
            })
        print(f"  {set_name} 推理完成 ({time.time()-t0:.1f}s)")

    summary: dict[str, dict] = {}
    for set_name in sets:
        rows = [r for r in all_rows if r["set"] == set_name]
        n = len(rows)
        attack = "attack" in set_name
        summary[set_name] = {"n": n, "kind": "attack" if attack else "benign"}
        for det, key in [("cn_bert", "cn_bert_hit"), ("deberta", "deberta_hit"), ("rules", "rule_hit")]:
            hits = sum(1 for r in rows if r[key])
            summary[set_name][det] = {"hits": hits,
                                      "detection_rate" if attack else "fp_rate": round(hits / n, 4)}

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / "cn_detector_compare.json"
    out.write_text(json.dumps({
        "run_at": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "config": {
            "jailbench": "STAIR-BUPT/JailBench 公开子集（完整 10800 条需申请），"
                         f"{'full' if args.full else f'stratified {JAILBENCH_N} seed={JAILBENCH_SEED}'}",
            "promptguard2": "meta-llama/Prompt-Guard-2-86M 经 hf-mirror 拉取 401（gated 许可墙），"
                            "按计划 §7 回退为 protectai/deberta-v3-base-prompt-injection-v2 作英文专用对照",
            "cn_bert_threshold": CN_BERT_THRESHOLD,
        },
        "summary": summary, "rows": all_rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入: {out}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
