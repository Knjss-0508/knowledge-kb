from pydantic import BaseModel, Field


class AnswerHubControlUpdate(BaseModel):
    enabled: bool | None = None
    schedule_enabled: bool | None = None
    schedule_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
