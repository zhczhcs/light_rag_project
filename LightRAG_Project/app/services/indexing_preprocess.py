from __future__ import annotations

import re
from typing import Any

from app.services.indexing_preprocess_rules import BLOCK_MIN_LINES_FOR_FILTERING
from app.services.indexing_preprocess_rules import BLOCK_PERCENT_ENCODED_THRESHOLD
from app.services.indexing_preprocess_rules import BLOCK_REFERENCE_HINT_THRESHOLD
from app.services.indexing_preprocess_rules import BLOCK_URL_DENSITY_THRESHOLD
from app.services.indexing_preprocess_rules import DOMAIN_ALIAS_RULES
from app.services.indexing_preprocess_rules import GRAPH_EXTRACTION_GUIDELINES
from app.services.indexing_preprocess_rules import INLINE_CITATION_REGEX
from app.services.indexing_preprocess_rules import LEADING_NOISE_REGEXES
from app.services.indexing_preprocess_rules import PERCENT_ENCODED_TOKEN_REGEX
from app.services.indexing_preprocess_rules import REFERENCE_DENSITY_MIN_LINES
from app.services.indexing_preprocess_rules import REFERENCE_DENSITY_MIN_MATCHES
from app.services.indexing_preprocess_rules import REFERENCE_DENSITY_THRESHOLD
from app.services.indexing_preprocess_rules import REFERENCE_LINE_REGEXES
from app.services.indexing_preprocess_rules import SYSTEM_BLOCK_MARKERS
from app.services.indexing_preprocess_rules import TAIL_HEADING_MIN_FRACTION
from app.services.indexing_preprocess_rules import TAIL_LINK_DENSITY_THRESHOLD
from app.services.indexing_preprocess_rules import TAIL_PERCENT_ENCODED_THRESHOLD
from app.services.indexing_preprocess_rules import TAIL_REFERENCE_HINT_THRESHOLD
from app.services.indexing_preprocess_rules import TAIL_SECTION_HEADINGS
from app.services.indexing_preprocess_rules import UNHEADED_REFERENCE_TAIL_MIN_FRACTION
from app.services.indexing_preprocess_rules import URL_REGEX


_REFERENCE_LINE_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in REFERENCE_LINE_REGEXES]
_INLINE_CITATION_PATTERN = re.compile(INLINE_CITATION_REGEX, re.IGNORECASE)
_LEADING_NOISE_PATTERNS = [re.compile(pattern, re.DOTALL) for pattern in LEADING_NOISE_REGEXES]
_URL_PATTERN = re.compile(URL_REGEX, re.IGNORECASE)
_PERCENT_ENCODED_PATTERN = re.compile(PERCENT_ENCODED_TOKEN_REGEX)


def _looks_like_reference_line(line: str) -> bool:
    text = (line or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _REFERENCE_LINE_PATTERNS)


def _looks_like_system_block(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in SYSTEM_BLOCK_MARKERS)


def _block_metrics(text: str) -> dict[str, float]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return {"line_count": 0.0, "url_density": 0.0, "percent_density": 0.0, "reference_density": 0.0}

    url_hits = sum(len(_URL_PATTERN.findall(line)) for line in lines)
    percent_hits = sum(len(_PERCENT_ENCODED_PATTERN.findall(line)) for line in lines)
    reference_hits = sum(1 for line in lines if _looks_like_reference_line(line))
    line_count = float(len(lines))
    return {
        "line_count": line_count,
        "url_density": url_hits / line_count,
        "percent_density": percent_hits / line_count,
        "reference_density": reference_hits / line_count,
    }


def _is_reference_tail(lines: list[str], start_index: int) -> bool:
    tail_lines = [tail_line for tail_line in lines[start_index + 1:] if tail_line.strip()]
    if not tail_lines:
        return True
    if len(tail_lines) < REFERENCE_DENSITY_MIN_LINES:
        return False

    reference_like_count = sum(1 for tail_line in tail_lines if _looks_like_reference_line(tail_line))
    density = reference_like_count / max(len(tail_lines), 1)
    tail_text = "\n".join(tail_lines)
    metrics = _block_metrics(tail_text)
    return (
        density >= REFERENCE_DENSITY_THRESHOLD
        or reference_like_count >= REFERENCE_DENSITY_MIN_MATCHES
        or metrics["url_density"] >= TAIL_LINK_DENSITY_THRESHOLD
        or metrics["percent_density"] >= TAIL_PERCENT_ENCODED_THRESHOLD
        or metrics["reference_density"] >= TAIL_REFERENCE_HINT_THRESHOLD
    )


def _find_unheaded_reference_tail_start(lines: list[str]) -> int | None:
    minimum_tail_start = max(3, int(len(lines) * UNHEADED_REFERENCE_TAIL_MIN_FRACTION))
    for index in range(minimum_tail_start, len(lines)):
        if not _looks_like_reference_line(lines[index]):
            continue
        if _is_reference_tail(lines, index - 1):
            return index
    return None


def _filter_noise_blocks(text: str, removals: list[dict[str, Any]]) -> str:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    if not blocks:
        return text.strip()

    kept_blocks: list[str] = []
    for block in blocks:
        if _looks_like_system_block(block):
            removals.append({"reason": "system_block", "preview": block[:200]})
            continue

        metrics = _block_metrics(block)
        if metrics["line_count"] >= BLOCK_MIN_LINES_FOR_FILTERING and (
            metrics["url_density"] >= BLOCK_URL_DENSITY_THRESHOLD
            or metrics["percent_density"] >= BLOCK_PERCENT_ENCODED_THRESHOLD
            or (
                metrics["reference_density"] >= BLOCK_REFERENCE_HINT_THRESHOLD
                and (metrics["url_density"] > 0 or metrics["percent_density"] > 0)
            )
        ):
            removals.append({"reason": "dense_reference_or_url_block", "preview": block[:200], "metrics": metrics})
            continue

        kept_blocks.append(block)

    return "\n\n".join(kept_blocks).strip()


def _strip_trailing_reference_sections(text: str) -> str:
    lines = text.splitlines()
    if len(lines) < 8:
        return text

    minimum_tail_start = max(3, int(len(lines) * TAIL_HEADING_MIN_FRACTION))
    for index, line in enumerate(lines):
        heading = line.strip().strip("#*:：.- ").lower()
        if heading not in TAIL_SECTION_HEADINGS:
            continue
        if index < minimum_tail_start:
            continue

        if _is_reference_tail(lines, index):
            return "\n".join(lines[:index]).strip()

    unheaded_start = _find_unheaded_reference_tail_start(lines)
    if unheaded_start is not None:
        return "\n".join(lines[:unheaded_start]).strip()

    return text


def _sanitize_extraction_noise(text: str, removals: list[dict[str, Any]] | None = None) -> str:
    content = text
    for pattern in _LEADING_NOISE_PATTERNS:
        updated = pattern.sub("", content, count=1)
        if updated != content and removals is not None:
            removals.append({"reason": "leading_noise", "preview": content[:200]})
        content = updated

    updated = _INLINE_CITATION_PATTERN.sub("", content)
    if updated != content and removals is not None:
        removals.append({"reason": "inline_citation_marker", "preview": content[:200]})
    content = updated
    content = re.sub(r"\n{3,}", "\n\n", content)
    content = re.sub(r"[ \t]{2,}", " ", content)
    content = re.sub(r"\s+([，。；：！？,.])", r"\1", content)
    return content.strip()


def build_indexing_guidance(text: str) -> dict[str, list[str]]:
    """保留术语归一和图谱提示的生成能力，但不再默认注入正文。"""
    content = (text or "").strip()
    if not content:
        return {"glossary_lines": [], "graph_hint_lines": []}

    lowered = content.lower()
    glossary_lines: list[str] = []
    for rule in DOMAIN_ALIAS_RULES:
        if not any(trigger.lower() in lowered for trigger in rule["triggers"]):
            continue
        aliases = ", ".join(rule["aliases"])
        glossary_lines.append(f"- {rule['canonical']} | aliases: {aliases}")

    graph_hint_lines = [f"- {line}" for line in GRAPH_EXTRACTION_GUIDELINES] if glossary_lines else []
    return {"glossary_lines": glossary_lines, "graph_hint_lines": graph_hint_lines}


def preprocess_indexing_text_dry_run(text: str) -> dict[str, Any]:
    original = (text or "").strip()
    removals: list[dict[str, Any]] = []
    content = _sanitize_extraction_noise(original, removals)
    stripped = _strip_trailing_reference_sections(content)
    if stripped != content:
        removals.append({"reason": "trailing_reference_section", "preview": content[-300:]})
    content = stripped
    filtered = _filter_noise_blocks(content, removals)
    guidance = build_indexing_guidance(filtered)
    return {
        "original_text": original,
        "cleaned_text": filtered,
        "removals": removals,
        "guidance": guidance,
    }


def preprocess_indexing_text(text: str) -> str:
    """保守清洗索引正文，不再把系统提示块直接注入文档内容。"""
    return preprocess_indexing_text_dry_run(text)["cleaned_text"]
