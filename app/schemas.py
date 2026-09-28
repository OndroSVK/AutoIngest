from __future__ import annotations

from pydantic import BaseModel, Field


class ChannelBase(BaseModel):
    display_name: str = Field(..., max_length=64)
    enabled: bool = False
    input_url: str
    output_folder: str = ""
    segment_duration: int = Field(300, ge=30, le=86400)
    automatic_recording: bool = True


class ChannelCreate(ChannelBase):
    pass


class ChannelUpdate(BaseModel):
    display_name: str | None = Field(None, max_length=64)
    enabled: bool | None = None
    input_url: str | None = None
    output_folder: str | None = None
    segment_duration: int | None = Field(None, ge=30, le=86400)
    automatic_recording: bool | None = None


class Channel(ChannelBase):
    id: int
    created_at: str
    updated_at: str


class ActionResult(BaseModel):
    ok: bool
    message: str
