"""All SQLAlchemy models, mirroring ESD §4. Import from here so Base.metadata
sees every table (Alembic autogenerate, tests)."""
from app.models.base import Base
from app.models.assessment import (
    AssessmentAnswer,
    AssessmentConversation,
    AssessmentMessage,
    CandidateQuestion,
    FunctionalSkillsReport,
    JobCompetency,
    ReportDimension,
    ReportSkillEvidence,
)
from app.models.bd import (
    CHANNELS,
    PROGRESS_FLAGS,
    PROGRESS_LABELS,
    SOCIAL_SOURCES,
    TENANT_PROSPECT,
    BDLead,
)
from app.models.billing import (
    CONSUMPTION_SUBUNITS,
    EVENT_COMPLETED,
    EVENT_GRANT,
    EVENT_INCOMPLETE,
    EVENT_NO_SHOW,
    EVENT_OLD_PROFILE_REVIEW,
    LEDGER_EVENT_TYPES,
    SUBUNITS_PER_CREDIT,
    BillingTransaction,
    CreditLedgerEntry,
    CreditLot,
    CreditLotDraw,
    CreditPurchase,
    OldProfileReview,
    WebhookEvent,
)
from app.models.agent import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_SUCCESS,
    AgentExecutionTrace,
    AgentToolApprovalRule,
)
from app.models.context import ContextChunk
from app.models.evidence import (
    EvidenceClaim,
    EvidenceClaimLink,
    EvidenceItemRow,
    PortableEvidenceItem,
)
from app.models.candidate_update import CandidateUpdate
from app.models.deletion import CandidateDeletionRequest
from app.models.support import SupportMessage, SupportThread
from app.models.bgv import BGVInquiry, BGVShareConsent
from app.models.bgv_documents import BGVContactCorrection, CandidateBGVDocument
from app.models.bgv_verification import BGVVerification
from app.models.conversation import (
    Conversation,
    ConversationAttachment,
    ConversationMessage,
    ConversationParticipant,
)
from app.models.employment import CandidateEmployment
from app.models.dual_mode import AssessmentConsent, VideoRecording, VideoRecordingSegment
from app.models.voice import VoiceAnswer
from app.models.project import CandidateProject
from app.models.background_job import BackgroundJob
from app.models.leadership import (
    JobLeadershipContext,
    LeadershipDepartmentExpectation,
    LeadershipProfile,
)
from app.models.department import CompanyDepartment
from app.models.candidate import (
    Candidate,
    CandidateTeamReview,
    Interview,
    JobCandidateLink,
    PipelineStatusEntry,
    Profile,
)
from app.models.company import Company, EmailTemplate, HiringManager
from app.models.compliance import (
    COMMERCIAL_DOCUMENT_TYPES,
    DOCUMENT_GROUPS,
    DOCUMENT_LABELS,
    DOCUMENT_TYPES,
    TAX_DOCUMENT_TYPES,
    ComplianceDocument,
)
from app.models.email_log import EMAIL_TYPES, EmailLog
from app.models.email_sender import ClientEmailSender
from app.models.enums import (
    APPROVAL_CHAIN,
    ApprovalDecision,
    JobStatus,
    LinkSource,
    PipelineStatus,
    Role,
    Tier,
    UserStatus,
)
from app.models.job import JDDraft, Job, JobApproval
from app.models.job_setup import (
    SWOT_ANALYSIS_EDITED,
    SWOT_ANALYSIS_FAILED,
    SWOT_ANALYSIS_GENERATED,
    SWOT_ANALYSIS_GENERATING,
    SWOT_ANALYSIS_NOT_GENERATED,
    SWOT_ANALYSIS_SECTIONS,
    SWOT_AREAS,
    SWOT_STATUS_ACTIVE,
    SWOT_STATUS_COMPLETE,
    JobMatchingCategory,
    JobSwotAnalysis,
    JobSwotIntake,
)
# The pause record the turn timer subtracts (migration 0125). Written by
# proctoring and the voice answer route; read by the conversation engine.
from app.models.assessment_pause import AssessmentPause
from app.models.proctoring import (
    ProctoringEvent,
    ProctoringReport,
    ProctoringSession,
)
from app.models.cost import (
    COST_BASIS_ESTIMATED,
    COST_BASIS_FINALIZED,
    COST_BASIS_VALUES,
    AssessmentCostRecord,
)
from app.models.telemetry import TelemetryEvent
from app.models.tenant import AuditLog, RolePermission, Tenant
from app.models.hiring import (
    CalibrationRecord,
    Evaluation,
    ReviewDisposition,
)
# The append-only record of which scorecard version a job was frozen against,
# and when. Read by `orchestration/versioning` to answer what a candidate
# applied under.
from app.models.job_scorecard_binding import JobScorecardBinding
# The immutable skills contract locked at a job's first assessment start
# (migration 0118). Read by `services/assessment_contract`.
from app.models.job_skill_snapshot import JobSkillSnapshot
# A coding question's answer key, its Run history and its final submission
# (migration 0124). The key is read and written only by
# `services/coding_assessment/keys`.
from app.models.coding import CodingQuestionKey, CodingRun, CodingSubmission
# A company's public self-registration before it is a customer (migration
# 0134). Read and written only by `api/company_onboarding`.
from app.models.company_registration import CompanyRegistration
from app.models.user import User

__all__ = [
    "JobLeadershipContext",
    "LeadershipDepartmentExpectation",
    "LeadershipProfile",
    "CompanyDepartment",
    "Base",
    "APPROVAL_CHAIN",
    "ApprovalDecision",
    "BDLead",
    "BGVContactCorrection",
    "BGVInquiry",
    "BGVShareConsent",
    "CHANNELS",
    "AssessmentAnswer",
    "AssessmentConsent",
    "AssessmentConversation",
    "AssessmentMessage",
    "VideoRecording",
    "VideoRecordingSegment",
    "AuditLog",
    "BillingTransaction",
    "Candidate",
    "CandidateProject",
    "BackgroundJob",
    "CandidateUpdate",
    "CandidateDeletionRequest",
    "SupportMessage",
    "SupportThread",
    "CandidateQuestion",
    "CandidateTeamReview",
    "CONSUMPTION_SUBUNITS",
    "CreditLedgerEntry",
    "CreditLot",
    "CreditLotDraw",
    "CreditPurchase",
    "EVENT_COMPLETED",
    "EVENT_GRANT",
    "EVENT_INCOMPLETE",
    "EVENT_NO_SHOW",
    "EVENT_OLD_PROFILE_REVIEW",
    "LEDGER_EVENT_TYPES",
    "OldProfileReview",
    "SUBUNITS_PER_CREDIT",
    "WebhookEvent",
    "COMMERCIAL_DOCUMENT_TYPES",
    "Company",
    "CompanyRegistration",
    "ComplianceDocument",
    "DOCUMENT_GROUPS",
    "DOCUMENT_LABELS",
    "DOCUMENT_TYPES",
    "EMAIL_TYPES",
    "ClientEmailSender",
    "EmailLog",
    "EmailTemplate",
    "HiringManager",
    "FunctionalSkillsReport",
    "Interview",
    "Job",
    "JobApproval",
    "JobMatchingCategory",
    "JobSwotAnalysis",
    "JobSwotIntake",
    "SWOT_ANALYSIS_EDITED",
    "SWOT_ANALYSIS_FAILED",
    "SWOT_ANALYSIS_GENERATED",
    "SWOT_ANALYSIS_GENERATING",
    "SWOT_ANALYSIS_NOT_GENERATED",
    "SWOT_ANALYSIS_SECTIONS",
    "SWOT_AREAS",
    "SWOT_STATUS_ACTIVE",
    "SWOT_STATUS_COMPLETE",
    "JobCandidateLink",
    "JobCompetency",
    "JobStatus",
    "LinkSource",
    "PipelineStatus",
    "PipelineStatusEntry",
    "PROGRESS_FLAGS",
    "PROGRESS_LABELS",
    "Profile",
    "Role",
    "SOCIAL_SOURCES",
    "TENANT_PROSPECT",
    "ReportDimension",
    "ReportSkillEvidence",
    "RolePermission",
    "TAX_DOCUMENT_TYPES",
    "AgentExecutionTrace",
    "CalibrationRecord",
    "Evaluation",
    "ReviewDisposition",
    "JobScorecardBinding",
    "JobSkillSnapshot",
    "CodingQuestionKey",
    "CodingRun",
    "CodingSubmission",
    "AgentToolApprovalRule",
    "ContextChunk",
    "EvidenceClaim",
    "EvidenceClaimLink",
    "EvidenceItemRow",
    "PortableEvidenceItem",
    "STATUS_FAILED",
    "STATUS_PARTIAL",
    "STATUS_SUCCESS",
    "AssessmentPause",
    "ProctoringEvent",
    "ProctoringReport",
    "ProctoringSession",
    "AssessmentCostRecord",
    "COST_BASIS_ESTIMATED",
    "COST_BASIS_FINALIZED",
    "COST_BASIS_VALUES",
    "TelemetryEvent",
    "Tenant",
    "Tier",
    "User",
    "UserStatus",
    "CandidateEmployment",
    "BGVVerification",
    "CandidateBGVDocument",
    "Conversation",
    "ConversationParticipant",
    "ConversationMessage",
    "ConversationAttachment",
    "VoiceAnswer",
]
