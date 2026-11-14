import os


class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://ccdev:ccdev@127.0.0.1:5432/ccdev"
    )
    # 测试时可通过该环境变量切换 schema，实现隔离
    search_schema: str = os.getenv("SEARCH_SCHEMA", "public")
    data_dir: str = os.getenv("DATA_DIR", "/workspace/20/data/sample")
    token_secret: str = os.getenv("TOKEN_SECRET", "dev-secret-textbook-search")
    token_ttl_hours: int = int(os.getenv("TOKEN_TTL_HOURS", "12"))
    bm25_k1: float = float(os.getenv("BM25_K1", "1.5"))
    bm25_b: float = float(os.getenv("BM25_B", "0.75"))
    auto_seed: bool = os.getenv("AUTO_SEED", "true").lower() == "true"
    long_paragraph_threshold: int = int(os.getenv("LONG_PARAGRAPH_THRESHOLD", "80"))


settings = Settings()
