"""配置中心：所有 Key、地址、开关都从 .env 文件读，代码里不写死任何密钥。

为什么单独搞一个文件？
    如果 API Key 写散在 10 个文件里，哪天换了 Key 你得改 10 个地方；
    而且一不小心把 Key 提交到 GitHub，钱就会被别人刷光。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从 .env 读取的配置。字段名必须和 .env 里的变量名一致（大小写不敏感）。"""

    # ---- LLM ----
    llm_api_key: str = "sk-请填写"
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"

    # ---- 向量库 ----
    chroma_dir: str = "./data/chroma"

    # ---- 数据库 ----
    redis_url: str = "redis://localhost:6379/0"
    mysql_url: str = "mysql+pymysql://root:password@localhost:3306/campus_agent"

    # ---- 服务 ----
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    # 告诉 pydantic：去当前目录的 .env 读配置；找不到就用上面的默认值
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


# 全局唯一的配置对象，其他文件 `from app.config import settings` 直接用
settings = Settings()
