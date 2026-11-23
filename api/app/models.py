"""Response/request schemas. These become the OpenAPI spec, which becomes the MCP tool contract,
so field descriptions here are written for both humans and LLMs."""
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class State(StrEnum):
    pending = "pending"
    running = "running"
    stopping = "stopping"
    stopped = "stopped"
    shutting_down = "shutting-down"
    terminated = "terminated"


class SortBy(StrEnum):
    vcpus = "vcpus"
    memory_gib = "memory_gib"
    launch_time = "launch_time"
    instance_id = "instance_id"


class GroupBy(StrEnum):
    region = "region"
    account_id = "account_id"
    state = "state"
    instance_type = "instance_type"
    instance_family = "instance_family"
    team = "team"
    service = "service"
    lifecycle = "lifecycle"


class Instance(BaseModel):
    snapshot_ts: datetime = Field(description="When this inventory snapshot was taken (UTC)")
    account_id: str
    region: str
    availability_zone: str | None
    instance_id: str
    instance_type: str
    instance_family: str | None
    vcpus: int | None
    memory_gib: float | None
    gpus: int | None
    state: str
    state_reason: str | None
    launch_time: datetime | None
    platform: str | None
    architecture: str | None
    lifecycle: str | None = Field(description="on-demand or spot")
    private_ip: str | None
    public_ip: str | None
    vpc_id: str | None
    subnet_id: str | None
    image_id: str | None
    name: str | None
    team: str | None
    service: str | None
    cost_center: str | None


class InstancePage(BaseModel):
    snapshot_ts: datetime | None = Field(description="Snapshot the results come from")
    total: int = Field(description="Total matching instances (all pages)")
    limit: int
    offset: int
    next_offset: int | None = Field(description="Pass as offset to get the next page; null when done")
    items: list[Instance]


class InstanceLookup(BaseModel):
    instance: Instance
    in_latest_snapshot: bool = Field(description="False if the instance no longer appears (e.g. terminated)")
    latest_snapshot_ts: datetime | None


class StateChange(BaseModel):
    snapshot_ts: datetime
    state: str
    instance_type: str
    state_reason: str | None


class InstanceHistory(BaseModel):
    instance_id: str
    first_seen: datetime | None
    last_seen: datetime | None
    changes: list[StateChange] = Field(description="Only snapshots where state or type changed")


class SummaryRow(BaseModel):
    key: str | None
    instances: int
    vcpus: int
    memory_gib: float
    gpus: int


class Summary(BaseModel):
    snapshot_ts: datetime | None
    group_by: GroupBy
    rows: list[SummaryRow]


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    scope: str
    issued_token_type: str | None = None


class NewUser(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=256)
    scopes: list[str] = ["inventory:read"]


class UserOut(BaseModel):
    username: str
    scopes: list[str]
    disabled: bool
    created_at: str
