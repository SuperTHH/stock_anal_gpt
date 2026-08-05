from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HENGCE_",
        env_file=".env",
        extra="ignore",
    )

    data_dir: Path = Path("data")
    tushare_token: SecretStr | None = Field(default=None, repr=False)
    timezone: str = "Asia/Shanghai"

    def ensure_local_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in ("raw", "normalized", "warehouse", "state", "reports"):
            (self.data_dir / name).mkdir(exist_ok=True)

    @classmethod
    def load(cls) -> "Settings":
        settings = cls()
        settings.ensure_local_dirs()
        return settings
