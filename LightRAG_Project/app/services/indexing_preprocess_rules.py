DOMAIN_ALIAS_RULES = [
    {
        "canonical": "网络空间主权",
        "triggers": ["网络空间主权", "网络主权", "cyberspace sovereignty", "cyber sovereignty"],
        "aliases": ["Cyberspace Sovereignty", "Cyber Sovereignty", "National Cyberspace Sovereignty"],
    },
    {
        "canonical": "国家主权",
        "triggers": ["国家主权", "national sovereignty"],
        "aliases": ["National Sovereignty"],
    },
    {
        "canonical": "网络空间安全",
        "triggers": ["网络空间安全", "网络安全", "网安", "cyberspace security", "cybersecurity", "information security"],
        "aliases": ["Cyberspace Security", "Cybersecurity", "Information Security"],
    },
    {
        "canonical": "考研",
        "triggers": ["考研", "研究生入学考试", "postgraduate entrance examination"],
        "aliases": ["Postgraduate Entrance Examination"],
    },
    {
        "canonical": "就业市场",
        "triggers": ["就业", "就业市场", "employment market"],
        "aliases": ["Employment Market"],
    },
    {
        "canonical": "国家战略",
        "triggers": ["国家战略", "战略急需", "national strategy"],
        "aliases": ["National Strategy"],
    },
]

GRAPH_EXTRACTION_GUIDELINES = [
    "Merge bilingual or synonymous mentions into the same canonical entity when the document context clearly refers to the same concept.",
    "Prefer stable concept entities over surface variants, abbreviations, or translated duplicates.",
    "Extract relations only when they are explicitly supported by the document text.",
    "When multiple passages describe the same entity or relation, keep one canonical node and attach multiple source passages.",
]

TAIL_SECTION_HEADINGS = {
    "参考文献",
    "references",
    "bibliography",
    "作者简介",
    "author biography",
    "about the authors",
    "致谢",
    "acknowledgement",
    "acknowledgments",
    "appendix",
    "附录",
}

SYSTEM_BLOCK_MARKERS = [
    "[Indexing Canonical Glossary]",
    "[Indexing Graph Hints]",
    "The following domain concepts refer to the same canonical entities during indexing.",
    "Use the following general graph extraction guidelines during indexing.",
]

LEADING_NOISE_REGEXES = [
    r"^好的，没问题。.*?以下是文档[^\n]*内容提取[:：]\s*",
    r"^我会按照你的要求[^\n]*[:：]\s*",
    r"^以下是文档[^\n]*内容提取[:：]\s*",
]

REFERENCE_LINE_REGEXES = [
    r"^\s*(?:\[?\d+\]?|\(\d+\))",
    r"\bet\s+al\.\b",
    r"doi(?::|\.org/)",
    r"\b(?:journal|conference|proceedings|ieee|acm|transactions)\b",
]

INLINE_CITATION_REGEX = r"\[cite(?:_start|:[^\]]+)?\]"

URL_REGEX = r"https?://|www\."
PERCENT_ENCODED_TOKEN_REGEX = r"(?:%[0-9A-Fa-f]{2}){3,}"

REFERENCE_DENSITY_MIN_LINES = 4
REFERENCE_DENSITY_THRESHOLD = 0.35
REFERENCE_DENSITY_MIN_MATCHES = 3

TAIL_HEADING_MIN_FRACTION = 0.55
UNHEADED_REFERENCE_TAIL_MIN_FRACTION = 0.55
TAIL_LINK_DENSITY_THRESHOLD = 0.20
TAIL_PERCENT_ENCODED_THRESHOLD = 0.08
TAIL_REFERENCE_HINT_THRESHOLD = 0.30

BLOCK_URL_DENSITY_THRESHOLD = 0.12
BLOCK_PERCENT_ENCODED_THRESHOLD = 0.05
BLOCK_REFERENCE_HINT_THRESHOLD = 0.25
BLOCK_MIN_LINES_FOR_FILTERING = 3