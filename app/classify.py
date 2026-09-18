"""Role-family taxonomy and explainable keyword classifier.

The nine launch families come from BRD section 7. Classification is scored,
deterministic and stores WHY a job landed in a family (matched keywords).
Families are ordered most-specific-first so ties resolve to the narrower role.
Jobs with no keyword evidence stay unclassified and surface in the /ops review
queue instead of being forced into a bucket.
"""
import re


FAMILIES = [
    {
        "slug": "genai-engineer",
        "name": "GenAI Engineer",
        "tagline": "LLM apps, agents, RAG and prompt-driven products",
        "keywords": [
            "genai", "gen ai", "generative ai", "llm", "llms",
            "large language model", "agentic", "ai agent", "ai agents",
            "agents sdk", "langchain", "llamaindex", "llama index", "rag",
            "retrieval augmented", "prompt engineering", "fine-tuning",
            "fine tuning", "finetuning", "openai", "anthropic", "gemini",
            "vector database", "vector db", "vector search", "mcp",
            "model context protocol", "a2a", "ai engineer", "ai/ml",
            "aiml", "artificial intelligence", "applied ai",
        ],
    },
    {
        "slug": "dl-engineer",
        "name": "DL Engineer",
        "tagline": "Deep learning training, models and GPU-era infrastructure",
        "keywords": [
            "deep learning", "pytorch", "tensorflow", "keras", "jax",
            "neural network", "neural nets", "cuda", "gpu kernels", "triton",
            "diffusion model", "training infrastructure", "distributed training",
            "inference optimization", "vllm", "model architecture",
        ],
    },
    {
        "slug": "ml-engineer",
        "name": "ML Engineer",
        "tagline": "Classical ML, MLOps and model deployment",
        "keywords": [
            "machine learning", "ml engineer", "mlops", "ml ops",
            "model deployment", "model training", "scikit", "sklearn",
            "xgboost", "lightgbm", "recommender", "recommendation system",
            "computer vision", "nlp", "natural language processing",
            "feature engineering", "onnx", "model monitoring",
            "predictive model",
        ],
    },
    {
        "slug": "data-engineer",
        "name": "Data Engineer",
        "tagline": "Pipelines, warehouses and analytics infrastructure",
        "keywords": [
            "data engineer", "data engineering", "etl", "elt",
            "data pipeline", "airflow", "spark", "pyspark", "databricks",
            "snowflake", "dbt", "data warehouse", "bigquery", "redshift",
            "hive", "data lake", "lakehouse", "stream processing",
        ],
    },
    {
        "slug": "cpp-engineer",
        "name": "C++ Engineer",
        "tagline": "Systems programming, embedded and low-latency work",
        "keywords": [
            "c++", "cpp", "systems programming", "system programming",
            "embedded", "low latency", "low-latency", "kernel", "operating systems",
            "dpdk", "ebpf", "compiler", "firmware", "rtos", "bare metal",
            "networking protocols", "hft", "high-frequency trading",
        ],
    },
    {
        "slug": "forward-deployed-engineer",
        "name": "Forward Deployed Engineer",
        "tagline": "Customer-facing engineers who ship solutions on-site in the product",
        "keywords": [
            "forward deployed", "forward-deployed", "forward deploying",
            "solutions engineer", "solution engineer", "solutions architect",
            "implementation engineer", "implementation consultant",
            "deployment engineer", "pre-sales engineer", "presales engineer",
            "field engineer", "technical account engineer",
        ],
    },
    {
        "slug": "gtm-engineer",
        "name": "GTM Engineer",
        "tagline": "Go-to-market engineering: growth, sales and rev-tech automation",
        "keywords": [
            "gtm engineer", "go-to-market engineer", "go to market",
            "growth engineer", "sales engineer", "revenue engineer",
            "developer relations", "devrel", "developer advocate",
            "developer advocacy", "marketing automation", "demand generation",
            "revops", "growth engineering",
        ],
    },
    {
        "slug": "backend-engineer",
        "name": "Backend Engineer",
        "tagline": "Servers, APIs and service-side product engineering",
        "keywords": [
            "backend", "back-end", "back end", "server side", "server-side",
            "api development", "microservice", "django", "flask", "fastapi",
            "spring boot", "spring", "node.js", "nodejs", "express",
            "ruby on rails", "laravel", "rest api", "graphql", "grpc",
            "web service",
        ],
    },
    {
        "slug": "sde",
        "name": "SDE",
        "tagline": "General software development engineering",
        "keywords": [
            "software engineer", "software developer", "software development engineer",
            "sde", "full stack", "full-stack", "fullstack", "web developer",
            "application developer", "member of technical staff", "mts",
            "programmer", "software development",
        ],
        # Bare 'developer'/'engineer' appear in almost every JD's prose, so
        # they only count when they appear in the title itself.
        "title_only": ["developer", "engineer"],
    },
]

# Title hits outweigh prose hits because titles are curated by the poster;
# title evidence is compared first, description evidence only breaks ties.
TITLE_WEIGHT = 5
JD_WEIGHT = 1


def _kw_pattern(kw: str):
    """Word-boundary pattern per keyword so 'rag' doesn't match 'storage',
    'mts' doesn't match 'payments', 'jax' doesn't match 'ajax'."""
    esc = re.escape(kw)
    lead = r"\b" if kw[0].isalnum() else ""
    trail = r"\b" if kw[-1:].isalnum() else ""
    return re.compile(lead + esc + trail)


for _fam in FAMILIES:
    _fam["patterns"] = [_kw_pattern(kw) for kw in _fam["keywords"]]
    _fam["title_only_patterns"] = [
        _kw_pattern(kw) for kw in _fam.get("title_only", [])
    ]


def _find_keywords(text: str, fam: dict, include_title_only: bool = True) -> list:
    if not text:
        return []
    low = text.lower()
    pairs = list(zip(fam["keywords"], fam["patterns"]))
    if include_title_only:
        pairs += list(zip(fam.get("title_only", []), fam["title_only_patterns"]))
    return [kw for kw, pat in pairs if pat.search(low)]


def classify(title: str, description: str = ""):
    """Return (family_slug, confidence, reason) or (None, None, reason).

    confidence: title hit => high (0.9), >=2 distinct JD hits => medium (0.6),
    exactly 1 JD hit => low (0.35) and we prefer to leave those unclassified
    unless the family is SDE (safe general bucket at low confidence).
    """
    best = None  # ((title_hits, jd_hits), family, t_hits, d_hits)
    for fam in FAMILIES:
        t_hits = _find_keywords(title, fam)
        d_hits = _find_keywords(description, fam, include_title_only=False)
        score = (len(t_hits), min(len(d_hits), 10))
        if score > (0, 0) and (best is None or score > best[0]):
            best = (score, fam, t_hits, d_hits)

    if best is None:
        return None, None, "no family keywords matched — needs review"

    _, fam, t_hits, d_hits = best
    parts = []
    if t_hits:
        parts.append("title matched: " + ", ".join(f"'{k}'" for k in t_hits[:5]))
    if d_hits:
        parts.append("description matched: " + ", ".join(f"'{k}'" for k in d_hits[:6]))
    reason = "; ".join(parts) if parts else "no evidence"

    if t_hits:
        return fam["slug"], 0.9, reason
    if len(d_hits) >= 2:
        return fam["slug"], 0.6, reason
    if fam["slug"] == "sde":
        return fam["slug"], 0.35, reason
    return None, None, "only weak description evidence (" + reason + ") — needs review"


# ---------------------------------------------------------------------------
# Skill extraction: normalized skill entities linked to requirement type.
# required = appears in the mandatory section, preferred = appears after a
# nice-to-have marker, inferred = found only in unstructured prose.
SKILLS = {
    # canonical name -> matching regex fragment
    "Python": r"\bpython\b",
    "Java": r"\bjava\b",
    "JavaScript": r"\bjavascript\b",
    "TypeScript": r"\btypescript\b",
    "Go": r"\bgo(?:lang)?\b",
    "Rust": r"\brust\b",
    "C": r"\bc\b(?!\+\+|#)",
    "C++": r"\bc\+\+|\bcpp\b",
    "C#": r"\bc#\b",
    "SQL": r"\bsql\b",
    "NoSQL": r"\bnosql\b",
    "Django": r"\bdjango\b",
    "Flask": r"\bflask\b",
    "FastAPI": r"\bfastapi\b|\bfast api\b",
    "Spring Boot": r"\bspring\s?boot\b",
    "Node.js": r"\bnode(?:\.js|js)?\b",
    "React": r"\breact(?:\.?js)?\b",
    "Next.js": r"\bnext(?:\.js|js)?\b",
    "Angular": r"\bangular\b",
    "Vue": r"\bvue(?:\.?js)?\b",
    "REST APIs": r"\brest(?:ful)?\s+api|\brest\b",
    "GraphQL": r"\bgraphql\b",
    "gRPC": r"\bgrpc\b",
    "Microservices": r"\bmicroservice",
    "PostgreSQL": r"\bpostgres(?:ql)?\b",
    "MySQL": r"\bmysql\b",
    "MongoDB": r"\bmongo(?:db)?\b",
    "Redis": r"\bredis\b",
    "Kafka": r"\bkafka\b",
    "RabbitMQ": r"\brabbitmq\b",
    "Spark": r"\bspark\b",
    "Airflow": r"\bairflow\b",
    "dbt": r"\bdbt\b",
    "Snowflake": r"\bsnowflake\b",
    "Databricks": r"\bdatabricks\b",
    "BigQuery": r"\bbigquery\b",
    "Elasticsearch": r"\belastic\s?search\b",
    "Vector DBs": r"\bvector\s+(?:database|db|store)",
    "AWS": r"\baws\b|\bamazon\s+web\s+services\b",
    "GCP": r"\bgcp\b|\bgoogle\s+cloud\b",
    "Azure": r"\bazure\b",
    "Docker": r"\bdocker\b",
    "Kubernetes": r"\bkubernetes\b|\bk8s\b",
    "Terraform": r"\bterraform\b",
    "Linux": r"\blinux\b",
    "CI/CD": r"\bci/cd\b|\bcicd\b|\bcontinuous\s+integration\b",
    "Git": r"\bgit\b(?!\shub)",
    "LLMs": r"\bllm|\blarge\s+language\s+model",
    "RAG": r"\brag\b|\bretrieval\s+augmented\b",
    "LangChain": r"\blangchain\b",
    "LlamaIndex": r"\bllama\s?index\b",
    "Prompt Engineering": r"\bprompt\s+engineering\b",
    "Agents": r"\bagentic|\bai\s+agents?\b|\bagents?\s+sdk\b",
    "PyTorch": r"\bpytorch\b",
    "TensorFlow": r"\btensorflow\b",
    "scikit-learn": r"\bscikit(?:-|\s)learn\b|\bsklearn\b",
    "Computer Vision": r"\bcomputer\s+vision\b|\bcnn\b|\bobject\s+detection\b",
    "NLP": r"\bnlp\b|\bnatural\s+language\s+processing\b",
    "Deep Learning": r"\bdeep\s+learning\b",
    "Machine Learning": r"\bmachine\s+learning\b|\bml\b",
    "Data Engineering": r"\bdata\s+engineering\b|\betl\b|\bdata\s+pipeline",
    "System Design": r"\bsystem\s+design\b|\bsystems\s+design\b",
    "Testing": r"\bunit\s+test|\bpytest\b|\bjest\b|\btest\s+coverage\b",
    "Embedded": r"\bembedded\b|\brtos\b|\bfirmware\b",
    "DPDK": r"\bdpdk\b",
    "OpenAI": r"\bopenai\b",
    "Gemini": r"\bgemini\b",
    "Hugging Face": r"\bhugging\s?face\b",
}

PREFERRED_MARKERS = [
    r"nice\s+to\s+have", r"good\s+to\s+have", r"bonus", r"preferred",
    r"plus(?:es)?\s*(?:point)?", r"desirable", r"advantageous",
]


def extract_skills(text: str) -> dict:
    """Return {canonical_skill: requirement_type} for a job description."""
    if not text:
        return {}
    low = text.lower()

    # Find where the preferred/nice-to-have section starts, if any.
    pref_start = len(low)
    for pat in PREFERRED_MARKERS:
        m = re.search(pat, low)
        if m:
            pref_start = min(pref_start, m.start())
    has_pref_section = pref_start < len(low)

    found = {}
    for name, frag in SKILLS.items():
        pat = re.compile(frag)
        if pat.search(low):
            first = pat.search(low).start()
            if has_pref_section and first >= pref_start:
                found[name] = "preferred"
            else:
                found[name] = "required"
    return found
