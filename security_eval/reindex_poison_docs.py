# -*- coding: utf-8 -*-
"""Phase 4 [D] 重建间接注入前置条件：以 sec_eval_attacker 身份重传 14 份投毒文档
并等待全部索引完成（共享向量库数据失效后恢复用）。

用法：python security_eval/reindex_poison_docs.py --base-url http://127.0.0.1:8001
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

ATTACKS = Path(__file__).resolve().parent / "attacks.jsonl"
ATTACKER = "sec_eval_attacker"
PASSWORD = "SecEval!2026"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8001")
    args = ap.parse_args()

    samples = [json.loads(l) for l in ATTACKS.read_text(encoding="utf-8").splitlines()
               if l.strip() and json.loads(l)["category"] == "indirect_injection"]
    # 去重（同文档可能服务多个样本）
    docs = {s["doc_filename"]: s["doc_content"] for s in samples}
    print(f"投毒文档 {len(docs)} 份，目标 {args.base_url}")

    tok = httpx.post(f"{args.base_url}/api/auth/login",
                     data={"username": ATTACKER, "password": PASSWORD}, timeout=30).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}

    pending = {}
    for attempt in range(4):
        failed = []
        for fn, content in docs.items():
            ok = False
            for retry in range(3):
                try:
                    r = httpx.post(f"{args.base_url}/api/upload", headers=h,
                                   files={"file": (fn, content.encode("utf-8"), "text/plain")}, timeout=120)
                    if r.status_code == 200:
                        pending[fn] = r.json().get("doc_id")
                        print(f"  已重传(强制重建索引): {fn} doc_id={pending[fn]}")
                        ok = True
                        break
                    print(f"  上传重试{retry+1} {fn}: {r.status_code}")
                except Exception as e:  # noqa: BLE001
                    print(f"  上传重试{retry+1} {fn}: {str(e)[:100]}")
                time.sleep(8)
            if not ok:
                failed.append(fn)
            time.sleep(3)
        if not failed:
            break
        print(f"  第{attempt+1}轮有失败，10s 后重试: {failed}")
        time.sleep(10)
    if len(pending) < len(docs):
        print(f"仍有 {len(docs)-len(pending)} 份上传失败: {set(docs)-set(pending)}")
        return 1

    deadline = time.time() + 3600
    remaining = set(pending)
    while remaining and time.time() < deadline:
        try:
            r = httpx.get(f"{args.base_url}/api/documents", headers=h, timeout=30)
            if r.status_code == 200:
                status_by_fn = {d.get("filename"): d.get("status") for d in r.json()}
                for fn in list(remaining):
                    st = status_by_fn.get(fn)
                    if st == "completed":
                        print(f"  ✅ 索引完成: {fn}")
                        remaining.discard(fn)
                    elif st == "failed":
                        print(f"  ❌ 索引失败: {fn}")
                        remaining.discard(fn)
        except Exception as e:  # noqa: BLE001
            print(f"  轮询异常: {str(e)[:80]}")
        if remaining:
            print(f"  等待中: {len(remaining)} 份 ... ({', '.join(sorted(remaining)[:3])})")
            time.sleep(20)

    if remaining:
        print(f"超时，未完成: {remaining}")
        return 2
    print("全部投毒文档索引完成，可重跑 indirect_injection")
    return 0


if __name__ == "__main__":
    sys.exit(main())
