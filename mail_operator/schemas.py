"""LLM 입출력 계약.

LLM 응답은 신뢰할 수 없는 입력입니다. 이 모듈의 모델을 통과한 값만
DB 저장, 태스크 생성, 답장 승인 흐름으로 전달합니다.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ACTION_TYPE_ALIASES = {
    "reply_needed": "reply_email",
    "task_todo": "create_reminder",
    "review_document": "create_reminder",
    "reminder": "create_reminder",
}


class ActionItem(BaseModel):
    """메일에서 추출할 수 있는 현재 구현 완료 액션."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal[
        "reply_email",
        "create_reminder",
    ]
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    priority: Literal["high", "medium", "low"] = "medium"
    deadline: Optional[str] = Field(default=None, max_length=50)
    related_attachment: Optional[str] = Field(default=None, max_length=255)

    @field_validator("type", mode="before")
    @classmethod
    def normalize_legacy_type(cls, value):
        return ACTION_TYPE_ALIASES.get(value, value)


class EmailAnalysis(BaseModel):
    """새 메일 한 건에 대한 검증된 분석 결과."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: str = Field(min_length=1, max_length=1000)
    urgency: Literal["urgent", "normal", "low"]
    priority_score: int = Field(ge=0, le=100)
    requires_response: bool
    action_items: List[ActionItem] = Field(default_factory=list, max_length=10)
    # 분석 단계에서는 답장 여부만 판단합니다. 실제 초안은 승인 흐름에서 원문을
    # 다시 넣고 별도 ReplyDraft 계약으로 생성합니다.
    draft_reply: None = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    warnings: List[str] = Field(
        default_factory=list,
        max_length=5,
        description="메일 데이터 자체의 누락 또는 모호함만 기록",
    )

    @field_validator("warnings")
    @classmethod
    def remove_prompt_internals(cls, values):
        blocked_terms = (
            "prompt",
            "프롬프트",
            "injection",
            "인젝션",
            "system message",
            "시스템 메시지",
            "python code",
            "python 코드",
            "untrusted_email_data",
        )
        return [
            value for value in values
            if not any(term in value.lower() for term in blocked_terms)
        ]


class ReplyDraft(BaseModel):
    """사용자 승인 전에 생성되는 답장 초안."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)


PreferenceRuleKind = Literal["processing_exclusion", "reply_style"]
PreferenceScopeType = Literal[
    "default",
    "sender_email",
    "sender_domain",
    "sender_name",
    "subject_contains",
    "current_sender",
    "current_domain",
]


class PreferenceProposal(BaseModel):
    """A bounded, inert preference proposal extracted from an authorized request."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: Literal["propose", "clarify"]
    rule_kind: Optional[PreferenceRuleKind] = None
    scope_type: Optional[PreferenceScopeType] = None
    scope_value: Optional[str] = Field(default=None, max_length=320)
    instruction: Optional[str] = Field(default=None, max_length=1000)
    response: Optional[str] = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_proposal(self):
        if self.mode == "clarify":
            if not self.response:
                raise ValueError("clarify mode requires a response")
            if any((self.rule_kind, self.scope_type, self.scope_value, self.instruction)):
                raise ValueError("clarify mode cannot include rule data")
            return self
        if self.rule_kind is None or self.scope_type is None:
            raise ValueError("propose mode requires rule_kind and scope_type")
        if self.scope_type not in {"default", "current_sender", "current_domain"}:
            if not self.scope_value:
                raise ValueError("this preference scope requires scope_value")
        if self.rule_kind == "reply_style" and not self.instruction:
            raise ValueError("reply_style requires an instruction")
        if self.rule_kind == "processing_exclusion" and self.instruction:
            raise ValueError("processing_exclusion cannot include an instruction")
        if self.response is not None:
            raise ValueError("propose mode cannot include a response")
        return self
