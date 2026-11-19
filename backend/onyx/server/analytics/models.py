import datetime

from pydantic import BaseModel


class QueryAnalyticsResponse(BaseModel):
    total_queries: int
    total_likes: int
    total_dislikes: int
    date: datetime.date


class UserAnalyticsResponse(BaseModel):
    total_active_users: int
    date: datetime.date


class OnyxbotAnalyticsResponse(BaseModel):
    total_queries: int
    auto_resolved: int
    date: datetime.date


class PersonaMessageAnalyticsResponse(BaseModel):
    total_messages: int
    date: datetime.date
    persona_id: int


class PersonaUniqueUsersResponse(BaseModel):
    unique_users: int
    date: datetime.date
    persona_id: int


class AgentDailyStats(BaseModel):
    date: datetime.date
    total_messages: int
    total_unique_users: int


class AgentStatsResponse(BaseModel):
    daily_stats: list[AgentDailyStats]
    total_messages: int
    total_unique_users: int
