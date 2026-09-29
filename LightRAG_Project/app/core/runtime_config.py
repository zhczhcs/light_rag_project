from dataclasses import dataclass
import os


TRUE_VALUES = {"1", "true", "yes", "on"}


def get_env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def get_env_int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    value = os.environ.get(name)
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default

    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def get_env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value is not None else default


@dataclass(frozen=True)
class QueryRuntimeConfig:
    chunk_top_k: int
    top_k: int
    max_entity_tokens: int
    max_relation_tokens: int
    max_total_tokens: int


def get_query_runtime_config() -> QueryRuntimeConfig:
    return QueryRuntimeConfig(
        chunk_top_k=get_env_int("QUERY_CHUNK_TOP_K", 6, minimum=1),
        top_k=get_env_int("QUERY_TOP_K", 10, minimum=1),
        max_entity_tokens=get_env_int("QUERY_MAX_ENTITY_TOKENS", 2000, minimum=1),
        max_relation_tokens=get_env_int("QUERY_MAX_RELATION_TOKENS", 3000, minimum=1),
        max_total_tokens=get_env_int("QUERY_MAX_TOTAL_TOKENS", 15000, minimum=1),
    )


@dataclass(frozen=True)
class AgenticRuntimeConfig:
    max_retries: int
    decompose_enabled: bool
    max_subqueries: int
    append_subqueries_to_main_query: bool
    reuse_query_as_keywords: bool
    subquery_concurrency: int
    subquery_enable_rerank: bool
    subquery_mode: str
    enable_neighbor_expansion: bool
    chunk_neighbors: int


def get_agentic_runtime_config() -> AgenticRuntimeConfig:
    return AgenticRuntimeConfig(
        max_retries=get_env_int("AGENTIC_MAX_RETRIES", 1, minimum=0),
        decompose_enabled=get_env_bool("AGENTIC_DECOMPOSE_ENABLED", True),
        max_subqueries=get_env_int("AGENTIC_MAX_SUBQUERIES", 3, minimum=0, maximum=4),
        append_subqueries_to_main_query=get_env_bool("AGENTIC_APPEND_SUBQUERIES_TO_MAIN_QUERY", True),
        reuse_query_as_keywords=get_env_bool("AGENTIC_REUSE_QUERY_AS_KEYWORDS", True),
        subquery_concurrency=get_env_int("AGENTIC_SUBQUERY_CONCURRENCY", 3, minimum=1),
        subquery_enable_rerank=get_env_bool("AGENTIC_SUBQUERY_ENABLE_RERANK", True),
        subquery_mode=get_env_str("AGENTIC_SUBQUERY_MODE", "naive"),
        enable_neighbor_expansion=get_env_bool("AGENTIC_ENABLE_NEIGHBOR_EXPANSION", True),
        chunk_neighbors=get_env_int("AGENTIC_CHUNK_NEIGHBORS", 1, minimum=0),
    )