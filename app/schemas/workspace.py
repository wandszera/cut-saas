from datetime import datetime
from pydantic import BaseModel, ConfigDict

class WorkspaceResponse(BaseModel):
    id: int
    name: str
    slug: str
    owner_user_id: int
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class WorkspaceMemberResponse(BaseModel):
    id: int
    workspace_id: int
    user_id: int
    role: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
